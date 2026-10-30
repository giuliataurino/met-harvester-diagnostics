"""Met Collection API client: departments, per-department id listings, one cached JSON file per
object. Thread-safe pacing so several workers share one request budget (the Met asks for
<= 80 req/s; default here is 20).

Cache layout (same <objectID>.json convention as met_harvester_diagnostics):
    <cache_dir>/objects/<objectID>.json      raw record exactly as served
    <cache_dir>/objects/<objectID>.missing   marker for a 404, so reruns skip it
    <cache_dir>/departments.json             last /departments response
    <cache_dir>/ids/<departmentId>.json      last /objects?departmentIds= listing
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API = "https://collectionapi.metmuseum.org/public/collection/v1"


class NotFound(Exception):
    """The API returned 404 for this object id."""


class RateLimiter:
    def __init__(self, rate: float):
        self.interval = 1.0 / max(rate, 0.01)
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)


@dataclass(frozen=True)
class Department:
    id: int
    name: str


class MetClient:
    def __init__(self, cache_dir: Path | str = "data/met", rate: float = 20,
                 session: Optional[requests.Session] = None, user_agent: str = "met-harvest-all",
                 verbose: bool = False):
        self.verbose = verbose
        self.requests_made = 0
        self.cache_dir = Path(cache_dir)
        self.objects_dir = self.cache_dir / "objects"
        self.ids_dir = self.cache_dir / "ids"
        for d in (self.objects_dir, self.ids_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.limiter = RateLimiter(min(rate, 80))
        self.http = session or requests.Session()
        if session is None:
            self.http.mount("https://", HTTPAdapter(pool_maxsize=32, max_retries=Retry(
                total=5, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=("GET",))))
        self.http.headers["User-Agent"] = user_agent

    # ---------------------------------------------------------------- transport
    def _get(self, path: str, params: dict | None = None) -> requests.Response:
        self.limiter.wait()
        r = self.http.get(f"{API}/{path}", params=params, timeout=60)
        self.requests_made += 1
        if self.verbose:
            import sys
            print(f"GET {r.url} -> {r.status_code}", file=sys.stderr, flush=True)
        if r.status_code == 404:
            raise NotFound(path)
        r.raise_for_status()
        return r

    # ---------------------------------------------------------------- departments
    def departments(self, refresh: bool = True) -> list[Department]:
        p = self.cache_dir / "departments.json"
        if not refresh and p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
        else:
            data = self._get("departments").json()
            data["fetched_at"] = _now()
            p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return [Department(d["departmentId"], d["displayName"]) for d in data.get("departments", [])]

    # ---------------------------------------------------------------- id listings
    def object_ids(self, department_id: Optional[int] = None, since: Optional[str] = None,
                   refresh: bool = True) -> list[int]:
        """Full id list for one department (or the whole collection when None). /objects lists
        completely; /v1.1/search stops at 10,000 so it is not used for enumeration.
        The listing is cached per department so a resumed harvest sees the same universe."""
        key = "all" if department_id is None else str(department_id)
        p = self.ids_dir / f"{key}.json"
        if not refresh and p.exists():
            return json.loads(p.read_text(encoding="utf-8")).get("objectIDs") or []
        params: dict = {}
        if department_id is not None:
            params["departmentIds"] = department_id
        if since:
            params["metadataDate"] = since
        data = self._get("objects", params or None).json()
        ids = data.get("objectIDs") or []
        p.write_text(json.dumps({"departmentId": department_id, "since": since, "total": data.get("total"),
                                 "count": len(ids), "fetched_at": _now(), "objectIDs": ids}),
                     encoding="utf-8")
        return ids

    # ---------------------------------------------------------------- objects
    def cache_path(self, object_id: int) -> Path:
        return self.objects_dir / f"{object_id}.json"

    def missing_path(self, object_id: int) -> Path:
        return self.objects_dir / f"{object_id}.missing"

    def is_cached(self, object_id: int) -> bool:
        return self.cache_path(object_id).exists() or self.missing_path(object_id).exists()

    def read_cached(self, object_id: int) -> Optional[dict]:
        p = self.cache_path(object_id)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return None

    def get_object(self, object_id: int, refresh: bool = False) -> dict:
        """Raw record, from cache unless refresh. A 404 writes a .missing marker and raises NotFound."""
        if not refresh:
            cached = self.read_cached(object_id)
            if cached is not None:
                return cached
            if self.missing_path(object_id).exists():
                raise NotFound(str(object_id))
        try:
            data = self._get(f"objects/{object_id}").json()
        except NotFound:
            self.missing_path(object_id).write_text(_now(), encoding="utf-8")
            raise
        if not isinstance(data, dict) or "objectID" not in data:
            raise ValueError(f"unexpected payload for {object_id}: {str(data)[:120]}")
        tmp = self.cache_path(object_id).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.cache_path(object_id))
        return data

    def check(self, department_id: int = 13) -> dict:
        """One live request of each kind, bypassing every cache, so a run can be shown to hit the API."""
        out = {"api": API}
        deps = self._get("departments").json().get("departments", [])
        out["departments"] = len(deps)
        listing = self._get("objects", {"departmentIds": department_id}).json()
        ids = listing.get("objectIDs") or []
        out["department"] = department_id
        out["listed_objects"] = listing.get("total", len(ids))
        if ids:
            rec = self._get(f"objects/{ids[0]}").json()
            out["sample_object"] = {k: rec.get(k) for k in ("objectID", "department", "title", "primaryImage", "metadataDate")}
        return out

    def iter_cached(self):
        """Yield every cached raw record (used by survey without re-touching the API)."""
        for p in sorted(self.objects_dir.glob("*.json"), key=lambda q: int(q.stem) if q.stem.isdigit() else 0):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and "objectID" in data:
                yield data


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
