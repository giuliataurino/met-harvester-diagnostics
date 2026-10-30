"""Minimal Met Collection API client: retries, one shared rate limiter for all threads, one JSON
file per object, 404 markers so missing ids are never asked for twice.

Cache layout (``cache_dir`` is the objects directory, as in the department-13 tool, so
``--cache data/met/objects`` still works; everything else lives next to it):

    data/met/objects/<objectID>.json      raw record, byte-for-byte as served
    data/met/objects/<objectID>.missing   the API returned 404 for this id
    data/met/ids/<departmentId>.json      the /objects listing for one department, per run
    data/met/departments.json             the /departments listing, refreshed on every run
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API = "https://collectionapi.metmuseum.org/public/collection/v1"


class MissingObject(Exception):
    """The Met returned 404 for this id (now, or on an earlier run)."""


class RateLimiter:
    """Thread-safe minimum interval between requests, shared by every worker."""

    def __init__(self, rate: float = 20):
        self.interval = 1 / max(rate, 0.1)
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.interval
        delay = slot - now
        if delay > 0:
            time.sleep(delay)


class MetClient:
    def __init__(self, cache_dir: Path, rate: float = 20, limiter: RateLimiter | None = None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.root = self.cache_dir.parent
        self.ids_dir = self.root / "ids"
        self.limiter = limiter or RateLimiter(rate)
        self.http = requests.Session()
        adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=Retry(
            total=5, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504)))
        self.http.mount("https://", adapter)
        self.http.headers["User-Agent"] = "met-diagnostics"

    # ------------------------------------------------------------------ requests
    def _get(self, path: str, params: dict | None = None) -> dict:
        self.limiter.wait()
        r = self.http.get(f"{API}/{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def departments(self) -> list[dict]:
        """``[{"departmentId": 1, "displayName": "American Decorative Arts"}, ...]``.
        Read live on every call and saved; the saved copy is used if the API is unreachable."""
        p = self.root / "departments.json"
        try:
            deps = self._get("departments").get("departments") or []
            self.root.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(deps, ensure_ascii=False, indent=1), encoding="utf-8")
            return deps
        except requests.RequestException:
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
            raise

    def object_ids(self, department_id: int, since: str | None = None) -> list[int]:
        """Always a live call, so reruns see new accessions; the listing is saved to
        ``ids/<N>.json`` so the id universe of a run is on disk. ``/objects`` lists a department
        completely; ``/v1.1/search`` would stop at the Met's 10,000-result paging limit.
        ``since`` (YYYY-MM-DD) is passed as ``metadataDate`` and restricts the list to objects the
        Met has updated on or after that date."""
        params = {"departmentIds": department_id}
        if since:
            params["metadataDate"] = since
        ids = self._get("objects", params).get("objectIDs") or []
        self.ids_dir.mkdir(parents=True, exist_ok=True)
        (self.ids_dir / f"{department_id}.json").write_text(
            json.dumps({"departmentId": department_id, "since": since, "total": len(ids), "objectIDs": ids}),
            encoding="utf-8")
        return ids

    # ------------------------------------------------------------------ cache
    def cache_path(self, object_id: int) -> Path:
        return self.cache_dir / f"{object_id}.json"

    def missing_path(self, object_id: int) -> Path:
        return self.cache_dir / f"{object_id}.missing"

    def get_object(self, object_id: int, refresh: bool = False) -> dict:
        """Raw record, from cache if present. A 404 writes a ``.missing`` marker and raises
        ``MissingObject``, as does an existing marker (no request is made).
        ``refresh=True`` relies on the live API instead: the cached copy and any ``.missing``
        marker are ignored, the record is fetched again and the file on disk is overwritten."""
        p = self.cache_path(object_id)
        if not refresh:
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
            if self.missing_path(object_id).exists():
                raise MissingObject(f"{object_id} returned 404 on an earlier run")
        try:
            data = self._get(f"objects/{object_id}")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                self.missing_path(object_id).touch()
                if refresh and p.exists():
                    p.unlink()          # the Met no longer serves it; do not keep a stale copy
                raise MissingObject(f"{object_id} returned 404") from e
            raise
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        if refresh and self.missing_path(object_id).exists():
            self.missing_path(object_id).unlink()
        return data

    def fetch_live(self, object_id: int) -> requests.Response:
        """Uncached call for the Live API tab."""
        return self.http.get(f"{API}/objects/{object_id}", timeout=30)
