"""
Met harvest diagnostics: a local web UI for inspecting what the Met Collection API returns for
every curatorial department and what the paa_graph_kb staging mapping makes of it.

    pip install -r requirements.txt
    python diagnostics/server.py [--cache data/met/objects] [--port 8765]

Then open http://localhost:8765. Self-contained: the client, record model, staging mapping and
AAT tables live in ../metdiag (copied from paa_graph_kb; see README for the drift caveat).

Scaled up from the department-13 tool: the harvest takes any set of departments (none = all),
runs several workers behind one rate limiter, resumes, and marks 404s so they are not retried; the
in-memory index keeps a small summary per object rather than the whole record, so ~480k objects
fit, and coverage is kept per department as records arrive.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from functools import lru_cache
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from rdflib import Graph

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # so `metdiag` imports without installing or symlinking

from metdiag import aat as aat_mappings  # noqa: E402
from metdiag.client import API, MetClient, MissingObject, RateLimiter  # noqa: E402
from metdiag.models import MetObject  # noqa: E402
from metdiag.staging import EXS, STG, met_object_to_staging  # noqa: E402

# The 58 fields documented at https://metmuseum.github.io/#object, in the documented order.
FIELDS = ("objectID isHighlight accessionNumber accessionYear isPublicDomain primaryImage primaryImageSmall "
          "additionalImages constituents department objectName title culture period dynasty reign portfolio "
          "artistRole artistPrefix artistDisplayName artistDisplayBio artistSuffix artistAlphaSort "
          "artistNationality artistBeginDate artistEndDate artistGender artistWikidata_URL artistULAN_URL "
          "objectDate objectBeginDate objectEndDate medium dimensions dimensionsParsed measurements creditLine "
          "geographyType city state county country region subregion locale locus excavation river "
          "classification rightsAndReproduction linkResource metadataDate repository objectURL tags "
          "objectWikidata_URL isTimelineWork GalleryNumber").split()
FIELD_BIT = {f: 1 << i for i, f in enumerate(FIELDS)}
AAT_FIELDS = (("culture", "cultures"), ("objectName", "object_types"),
              ("classification", "classifications"), ("medium", "materials"))
SEARCH_KEYS = ("title", "objectName", "culture", "medium", "accessionNumber", "period", "classification",
               "artistDisplayName", "department")
NO_DEPARTMENT = "(no department)"


def present(v) -> bool:
    return not (v is None or v == "" or v == [] or v == {})


def aat_term(field: str, value: str) -> str:
    return value.split(",")[0].strip() if field == "culture" else value


@lru_cache(maxsize=200_000)
def aat_uri(term: str, category: str) -> str | None:
    return aat_mappings.get_aat_uri(term, category=category)


# --------------------------------------------------------------------------- cache index
class Summary:
    """What the index keeps per object: enough for the grid, search, filters and coverage.
    The full record is read from disk when one object is opened."""
    __slots__ = ("objectID", "title", "objectName", "culture", "objectDate", "accessionNumber",
                 "primaryImageSmall", "department", "mask", "extra", "terms", "search", "file")

    def __init__(self, rec: dict, file: Path | None = None):
        self.objectID = rec["objectID"]
        self.title = rec.get("title") or None
        self.objectName = rec.get("objectName") or None
        self.culture = rec.get("culture") or None
        self.objectDate = rec.get("objectDate") or None
        self.accessionNumber = rec.get("accessionNumber") or None
        self.primaryImageSmall = rec.get("primaryImageSmall") or None
        self.department = rec.get("department") or NO_DEPARTMENT
        mask = 0
        extra = []
        for k, v in rec.items():
            if not present(v):
                continue
            bit = FIELD_BIT.get(k)
            if bit:
                mask |= bit
            else:
                extra.append(k)           # a key the Met serves that is not documented
        self.mask = mask
        self.extra = tuple(extra)
        self.terms = tuple(aat_term(f, rec[f]) if present(rec.get(f)) else None for f, _ in AAT_FIELDS)
        self.search = " ".join(str(rec.get(k, "")) for k in SEARCH_KEYS if rec.get(k)).lower()
        self.file = file

    def filled(self, field: str) -> bool:
        return bool(self.mask & FIELD_BIT.get(field, 0))

    def card(self) -> dict:
        return {k: getattr(self, k) for k in ("objectID", "title", "objectName", "culture", "objectDate",
                                              "accessionNumber", "primaryImageSmall", "department")}


class DeptStats:
    __slots__ = ("n", "fields", "extra", "terms")

    def __init__(self):
        self.n = 0
        self.fields = Counter()
        self.extra = Counter()
        self.terms = [Counter() for _ in AAT_FIELDS]

    def add(self, s: Summary, sign: int = 1) -> None:
        self.n += sign
        for f, bit in FIELD_BIT.items():
            if s.mask & bit:
                self.fields[f] += sign
        for k in s.extra:
            self.extra[k] += sign
        for i, t in enumerate(s.terms):
            if t:
                self.terms[i][t] += sign


class Cache:
    """Index over the objects directory. ``refresh()`` rescans the disk (in a background thread
    at startup); ``add()`` indexes a record the harvest just fetched."""

    def __init__(self, path: Path):
        self.path = path
        self.records: dict[int, Summary] = {}
        self.stats: dict[str, DeptStats] = defaultdict(DeptStats)
        self.lock = threading.Lock()
        self.indexing = False
        self.indexed_files = 0

    # -- reading ------------------------------------------------------------
    def read(self, object_id: int) -> dict | None:
        s = self.records.get(object_id)
        p = s.file if s and s.file else self.path / f"{object_id}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    # -- writing ------------------------------------------------------------
    def _put(self, s: Summary) -> None:
        old = self.records.get(s.objectID)
        if old:
            self.stats[old.department].add(old, -1)
        self.records[s.objectID] = s
        self.stats[s.department].add(s)

    def add(self, rec: dict, file: Path | None = None) -> None:
        s = Summary(rec, file)
        with self.lock:
            self._put(s)

    def refresh(self) -> int:
        """Rebuild the index from every ``*.json`` in the cache. Files named ``<objectID>.json``
        and files named by request hash (paa_graph_kb's layout) are both read, since records are
        identified by content."""
        self.indexing = True
        found: dict[int, Summary] = {}
        n = 0
        try:
            for f in (self.path.glob("*.json") if self.path.exists() else []):
                n += 1
                if n % 2000 == 0:
                    self.indexed_files = n
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(data, dict) and isinstance(data.get("objectID"), int):
                    found[data["objectID"]] = Summary(data, f)
            stats: dict[str, DeptStats] = defaultdict(DeptStats)
            for s in found.values():
                stats[s.department].add(s)
            with self.lock:
                self.records, self.stats = found, stats
        finally:
            self.indexing = False
            self.indexed_files = n
        return len(found)

    def missing_count(self) -> int:
        return sum(1 for _ in self.path.glob("*.missing")) if self.path.exists() else 0

    # -- queries ------------------------------------------------------------
    def snapshot(self) -> list[Summary]:
        with self.lock:
            return list(self.records.values())

    def departments(self) -> list[dict]:
        with self.lock:
            return sorted(({"department": d, "objects": st.n} for d, st in self.stats.items() if st.n),
                          key=lambda x: x["department"])


# --------------------------------------------------------------------------- harvest
class Harvest:
    """All departments, several workers, one rate limiter. Per-department progress rows; a
    department is marked done in ``harvest_state.json`` when every listed id has been handled and
    is skipped next time unless asked to redo. Failures go to ``harvest_failures.jsonl``."""

    def __init__(self, cache: Cache):
        self.cache = cache
        self.root = cache.path.parent
        self.state_file = self.root / "harvest_state.json"
        self.failures_file = self.root / "harvest_failures.jsonl"
        self.state = self._idle_state()
        self.stop_flag = False
        self.lock = threading.Lock()
        self.file_lock = threading.Lock()   # harvest_state.json is read-modify-write from several workers
        self.thread: threading.Thread | None = None

    @staticmethod
    def _idle_state() -> dict:
        return {"running": False, "paused": False, "done": 0, "total": 0, "fetched": 0, "cached": 0,
                "missing": 0, "failed": 0, "invalid": 0, "skipped": [], "started": None, "finished": None,
                "departments": [], "workers": 0, "rate": 0, "refresh": False, "since": None, "error": None}

    def done_departments(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8")).get("done", {})
        except (OSError, json.JSONDecodeError):
            return {}

    def _mark_done(self, dept_id: int, total: int) -> None:
        with self.file_lock:
            done = self.done_departments()
            done[str(dept_id)] = {"total": total, "when": time.strftime("%Y-%m-%dT%H:%M:%S")}
            self.root.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({"done": done}, indent=1), encoding="utf-8")

    def _log_failure(self, dept_id: int, oid: int, kind: str, error: str) -> None:
        with self.file_lock:
            with self.failures_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"departmentId": dept_id, "objectID": oid, "kind": kind,
                                     "error": error[:300], "when": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")

    def start(self, department_ids: list[int], limit: int | None, workers: int = 4, rate: float = 20,
              redo: bool = False, refresh: bool = False, since: str | None = None) -> bool:
        """``refresh`` relies on the live API for every id: the cache is ignored and overwritten.
        ``since`` (YYYY-MM-DD) asks the Met only for objects updated since that date and refetches
        those; both imply redo, since the point is to see what the API serves now."""
        if self.state["running"]:
            return False
        if since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
            raise ValueError("since must be YYYY-MM-DD")
        self.stop_flag = False
        self.state = {**self._idle_state(), "running": True, "started": time.time(),
                      "workers": max(1, min(workers, 16)), "rate": max(1, min(rate, 80)),
                      "refresh": refresh or bool(since), "since": since or None}
        self.thread = threading.Thread(target=self._run, args=(department_ids, limit, redo or refresh or bool(since)),
                                       daemon=True)
        self.thread.start()
        return True

    def _run(self, department_ids: list[int], limit: int | None, redo: bool) -> None:
        st = self.state
        client = MetClient(self.cache.path, limiter=RateLimiter(st["rate"]))
        rows: dict[int, dict] = {}
        q: queue.Queue = queue.Queue(maxsize=st["workers"] * 8)
        try:
            deps = client.departments()
            selected = [d for d in deps if not department_ids or d["departmentId"] in department_ids]
            for d in selected:
                rows[d["departmentId"]] = {"departmentId": d["departmentId"], "name": d["displayName"],
                                           "status": "queued", "total": 0, "done": 0, "fetched": 0,
                                           "cached": 0, "missing": 0, "failed": 0, "invalid": 0}
            st["departments"] = list(rows.values())
            already = self.done_departments()
            threads = [threading.Thread(target=self._worker, args=(client, q, rows), daemon=True)
                       for _ in range(st["workers"])]
            for t in threads:
                t.start()
            for d in selected:
                did = d["departmentId"]
                row = rows[did]
                if self.stop_flag:
                    row["status"] = "stopped"
                    continue
                if not redo and str(did) in already:
                    row["status"] = "skipped (done earlier)"
                    continue
                row["status"] = "listing"
                try:
                    ids = client.object_ids(did, since=st["since"])
                except Exception as e:  # noqa: BLE001
                    row["status"] = f"listing failed: {str(e)[:120]}"
                    continue
                if limit:
                    ids = ids[:limit]
                row["total"] = len(ids)
                with self.lock:
                    st["total"] += len(ids)
                if not ids:
                    row["status"] = "done" if not st["since"] else "done (nothing updated)"
                    if not limit and not st["since"]:
                        self._mark_done(did, 0)
                    continue
                row["status"] = "running"
                for oid in ids:
                    while st["paused"] and not self.stop_flag:
                        time.sleep(0.3)
                    if self.stop_flag:
                        break
                    q.put((did, oid, bool(limit)))
                if self.stop_flag:
                    row["status"] = "stopped"
            for _ in threads:
                q.put(None)
            for t in threads:
                t.join()
        except Exception as e:  # noqa: BLE001
            st["error"] = str(e)
        finally:
            st["running"] = False
            st["finished"] = time.time()

    def _worker(self, client: MetClient, q: queue.Queue, rows: dict[int, dict]) -> None:
        st = self.state
        while True:
            item = q.get()
            if item is None:
                return
            did, oid, limited = item
            row = rows[did]
            if self.stop_flag:
                self._count(row, "done")
                continue
            while st["paused"] and not self.stop_flag:
                time.sleep(0.3)
            in_cache = client.cache_path(oid).exists() and not st["refresh"]
            try:
                raw = client.get_object(oid, refresh=st["refresh"])
                self._count(row, "cached" if in_cache else "fetched")
                try:
                    MetObject.model_validate(raw)   # surface validation problems as the harvester would
                except Exception as e:  # noqa: BLE001
                    self._count(row, "invalid")
                    self._log_failure(did, oid, "invalid", str(e))
                    self._skip(oid, row["name"], f"model rejected: {e}")
                self.cache.add(raw)
            except MissingObject:
                self._count(row, "missing")
            except Exception as e:  # noqa: BLE001
                self._count(row, "failed")
                self._log_failure(did, oid, "failed", str(e))
                self._skip(oid, row["name"], str(e))
            finally:
                done = self._count(row, "done")
                if done >= row["total"] and row["status"] == "running":
                    row["status"] = "done" if not self.stop_flag else "stopped"
                    if not limited and not self.stop_flag and row["failed"] == 0 and not st["since"]:
                        self._mark_done(did, row["total"])   # a since-run is partial; never marks done

    def _count(self, row: dict, key: str) -> int:
        with self.lock:
            row[key] += 1
            self.state[key] += 1
            return row["done"]

    def _skip(self, oid: int, dept: str, error: str) -> None:
        with self.lock:
            if len(self.state["skipped"]) < 200:
                self.state["skipped"].append({"objectID": oid, "department": dept, "error": error[:200]})


# --------------------------------------------------------------------------- diagnostics
def object_detail(raw: dict) -> dict:
    obj = MetObject.model_validate(raw)
    g = Graph()
    g.bind("stg", STG)
    g.bind("exs", EXS)
    met_object_to_staging(g, obj)
    s = EXS[f"o/met:{obj.objectID}"]
    staged_props = sorted({str(p).split("#")[-1] for p in g.predicates(s)})
    aat = []
    for field, category in AAT_FIELDS:
        value = getattr(obj, field)
        if value:
            aat.append({"field": field, "term": aat_term(field, value),
                        "uri": aat_uri(aat_term(field, value), category)})
    return {
        "raw": raw,
        "fields": {k: present(v) for k, v in raw.items()},
        "undocumented": sorted(k for k in raw if k not in FIELD_BIT),
        "turtle": g.serialize(format="turtle"),
        "triples": len(g),
        "staged_properties": staged_props,
        "aat": aat,
        "nodes": {"images": len(list(g.objects(s, STG.hasImage))),
                  "measurements": len(list(g.objects(s, STG.hasMeasurement))),
                  "persons": len(list(g.objects(s, STG.hasPerson))),
                  "subjects": len(list(g.objects(s, STG.subject)))},
    }


def live_compare(object_id: int, cached: dict | None) -> dict:
    """Fetch the object straight from the Met (no MetClient, no cache) and compare it with
    what the harvester holds: the cached JSON, the MetObject view of it, and the staged triples."""
    r = requests.get(f"{API}/objects/{object_id}", timeout=30, headers={"User-Agent": "met-diagnostics"})
    out = {"status": r.status_code, "api": None, "cached": cached}
    if r.status_code != 200:
        out["error"] = f"API returned HTTP {r.status_code}"
        return out
    api = r.json()
    out["api"] = api

    # 1. cache vs live API: fields whose value differs (stale cache, or the Met edited the record)
    if cached:
        keys = sorted(set(api) | set(cached))
        out["cache_diff"] = [{"field": k, "api": api.get(k), "cached": cached.get(k)}
                             for k in keys if api.get(k) != cached.get(k)]
        out["cache_metadataDate"] = cached.get("metadataDate")
    out["api_metadataDate"] = api.get("metadataDate")

    # 2. live API vs model: keys the model has no field for, and values the model changed
    obj = MetObject.model_validate(api)
    dumped = json.loads(obj.model_dump_json())
    out["model_unknown_keys"] = sorted(k for k in api if k not in MetObject.model_fields)
    out["model_changed"] = [{"field": k, "api": api.get(k), "model": dumped.get(k)}
                            for k in MetObject.model_fields
                            if api.get(k) not in (None, "", [], {}) and dumped.get(k) != api.get(k)
                            and not _equivalent(api.get(k), dumped.get(k))]

    # 3. model vs staging: filled model fields that produced no triple at all
    g = Graph(); met_object_to_staging(g, obj)
    ttl = g.serialize(format="turtle")
    out["turtle"] = ttl
    out["triples"] = len(g)
    out["unstaged"] = [k for k, v in dumped.items() if present(v) and not _value_in_turtle(v, ttl)]
    return out


def _equivalent_loose(a, b) -> bool:
    """For comparing a raw API value with a model-dumped one: blanks and ints normalise."""
    blank = (None, "", [], {})
    return (a in blank and b in blank) or _equivalent(a, b)


def _equivalent(a, b) -> bool:
    """Same data despite model normalisation: '1956' == 1956, blanks pruned from lists,
    and sub-records that only gained absent optional keys as null."""
    if a == b:
        return True
    if isinstance(a, str) and isinstance(b, int):
        return a.strip() == str(b)
    if isinstance(a, list) and isinstance(b, list):
        a2 = [x for x in a if x not in ("", None)]
        b2 = [x for x in b if x not in ("", None)]
        return len(a2) == len(b2) and all(_equivalent(x, y) for x, y in zip(a2, b2))
    if isinstance(a, dict) and isinstance(b, dict):
        return all(_equivalent_loose(a.get(k), b.get(k)) for k in set(a) | set(b))
    return False


def _value_in_turtle(v, ttl: str) -> bool:
    if isinstance(v, bool):
        return True  # booleans are always staged
    if isinstance(v, (int, float)):
        return str(v) in ttl
    if isinstance(v, str):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", v):  # xsd:dateTime is reserialised; match the date part
            return v[:10] in ttl
        return v.replace('"', '\\"')[:40] in ttl
    if isinstance(v, list):
        return any(_value_in_turtle(x, ttl) for x in v) if v else True
    if isinstance(v, dict):
        return any(_value_in_turtle(x, ttl) for x in v.values()) if v else True
    return True


def _aat_block(terms: Counter, category: str, unmapped_limit: int = 40) -> dict:
    rows = [{"term": t, "count": c, "uri": aat_uri(t, category)} for t, c in terms.most_common()]
    objects = sum(r["count"] for r in rows)
    mapped = sum(r["count"] for r in rows if r["uri"])
    return {"distinct": len(rows), "objects": objects, "mapped_objects": mapped,
            "unmapped": [r for r in rows if not r["uri"]][:unmapped_limit]}


def coverage(cache: Cache, department: str | None = None) -> dict:
    """Field fill and AAT resolution, for one department or for the whole cache."""
    with cache.lock:
        stats = [cache.stats[department]] if department and department in cache.stats else \
            ([] if department else [st for st in cache.stats.values() if st.n])
        n = sum(st.n for st in stats)
        fields = Counter()
        extra = Counter()
        terms = [Counter() for _ in AAT_FIELDS]
        for st in stats:
            fields.update(st.fields)
            extra.update(st.extra)
            for i, c in enumerate(st.terms):
                terms[i].update(c)
    aat = {f: _aat_block(terms[i], category) for i, (f, category) in enumerate(AAT_FIELDS)}
    return {"objects": n, "department": department or "all",
            "fields": {f: fields.get(f, 0) for f in FIELDS}, "undocumented": dict(extra.most_common()), "aat": aat}


def coverage_matrix(cache: Cache) -> dict:
    """Fill rate of every documented field in every department, plus per-department AAT
    resolution for the four looked-up fields. The Coverage tab's department comparison."""
    with cache.lock:
        depts = sorted((d for d, st in cache.stats.items() if st.n), key=lambda d: -cache.stats[d].n)
        rows = []
        for d in depts:
            st = cache.stats[d]
            rows.append({"department": d, "objects": st.n,
                         "fill": [round(st.fields.get(f, 0) / st.n, 4) for f in FIELDS],
                         "mean_fill": round(sum(st.fields.get(f, 0) for f in FIELDS) / (st.n * len(FIELDS)), 4),
                         "never_filled": [f for f in FIELDS if not st.fields.get(f)],
                         "always_filled": [f for f in FIELDS if st.fields.get(f) == st.n],
                         "undocumented": sorted(st.extra),
                         "terms": [Counter(c) for c in st.terms]})
    for r in rows:
        aat = {}
        for i, (f, category) in enumerate(AAT_FIELDS):
            c = r["terms"][i]
            objects = sum(c.values())
            mapped = sum(k for t, k in c.items() if aat_uri(t, category))
            aat[f] = {"objects": objects, "mapped_objects": mapped, "distinct": len(c)}
        r["aat"] = aat
        del r["terms"]
    return {"fields": FIELDS, "departments": rows}


def norm(acc: str) -> str:
    return re.sub(r"\s+", "", acc).lower()


def perseus_matches(records: list[Summary], perseus_ttl: Path) -> dict:
    g = Graph().parse(perseus_ttl, format="turtle")
    perseus = {}
    for s in g.subjects(STG.institution):
        if "Metropolitan" in str(g.value(s, STG.institution)):
            acc = g.value(s, STG.accessionNumber)
            if acc:
                perseus[norm(str(acc))] = str(acc)
    met = {norm(r.accessionNumber): r for r in records if r.accessionNumber}
    matched = {k: v for k, v in perseus.items() if k in met}
    unmatched = sorted(v for k, v in perseus.items() if k not in met)
    by_dept = Counter(met[k].department for k in matched)
    return {"perseus_met_objects": len(perseus), "matched": len(matched), "unmatched": unmatched,
            "by_department": dict(by_dept.most_common()),
            "sample": [{"accession": perseus[k], "objectID": met[k].objectID, "department": met[k].department}
                       for k in list(matched)[:20]]}


# --------------------------------------------------------------------------- HTTP
def make_handler(cache: Cache, harvest: Harvest):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(HERE), **kw)

        def log_message(self, *a):  # quiet
            pass

        def send_json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/":
                self.path = "/index.html"
                return super().do_GET()
            if u.path == "/api/status":
                return self.send_json({**harvest.state, "in_cache": len(cache.records), "cache_dir": str(cache.path),
                                       "indexing": cache.indexing, "indexed_files": cache.indexed_files,
                                       "cache_departments": cache.departments()})
            if u.path == "/api/departments":
                try:
                    deps = MetClient(cache.path).departments()
                except Exception as e:  # noqa: BLE001
                    return self.send_json({"error": f"could not list departments: {e}", "departments": []}, 502)
                done = harvest.done_departments()
                in_cache = {d["department"]: d["objects"] for d in cache.departments()}
                return self.send_json({"departments": [
                    {**d, "done": done.get(str(d["departmentId"])), "in_cache": in_cache.get(d["displayName"], 0)}
                    for d in deps]})
            if u.path == "/api/objects":
                recs = cache.snapshot()
                text = q.get("q", "").lower()
                if text:
                    recs = [r for r in recs if text in r.search or text == str(r.objectID)]
                if q.get("department"):
                    recs = [r for r in recs if r.department == q["department"]]
                if q.get("missing"):
                    recs = [r for r in recs if not r.filled(q["missing"])]
                if q.get("images") == "1":
                    recs = [r for r in recs if r.primaryImageSmall]
                recs.sort(key=lambda r: r.objectID)
                off, lim = int(q.get("offset", 0)), int(q.get("limit", 60))
                return self.send_json({"total": len(recs), "items": [r.card() for r in recs[off:off + lim]]})
            m = re.fullmatch(r"/api/object/(\d+)", u.path)
            if m:
                rec = cache.read(int(m.group(1)))
                if not rec:
                    return self.send_json({"error": "not in cache"}, 404)
                try:
                    return self.send_json(object_detail(rec))
                except Exception as e:  # noqa: BLE001
                    return self.send_json({"error": f"staging failed: {e}", "raw": rec}, 500)
            m = re.fullmatch(r"/api/live/(\d+)", u.path)
            if m:
                oid = int(m.group(1))
                try:
                    return self.send_json(live_compare(oid, cache.read(oid)))
                except requests.RequestException as e:
                    return self.send_json({"error": f"could not reach the Met API: {e}"}, 502)
                except Exception as e:  # noqa: BLE001
                    return self.send_json({"error": str(e)}, 500)
            if u.path == "/api/coverage":
                return self.send_json(coverage(cache, q.get("department") or None))
            if u.path == "/api/coverage/matrix":
                return self.send_json(coverage_matrix(cache))
            if u.path == "/api/perseus":
                p = Path(q.get("path", ""))
                if not p.exists():
                    return self.send_json({"error": f"file not found: {p}"}, 404)
                return self.send_json(perseus_matches(cache.snapshot(), p))
            return super().do_GET()

        def do_POST(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/api/harvest":
                deps = [int(x) for x in re.split(r"[,\s]+", q.get("departments", "")) if x.strip().isdigit()]
                try:
                    ok = harvest.start(deps,
                                       int(q["limit"]) if q.get("limit") else None,
                                       workers=int(q.get("workers") or 4),
                                       rate=float(q.get("rate") or 20),
                                       redo=q.get("redo") == "1",
                                       refresh=q.get("refresh") == "1",
                                       since=q.get("since") or None)
                except ValueError as e:
                    return self.send_json({"started": False, "error": str(e)}, 400)
                return self.send_json({"started": ok})
            if u.path == "/api/pause":
                harvest.state["paused"] = not harvest.state["paused"]
                return self.send_json({"paused": harvest.state["paused"]})
            if u.path == "/api/stop":
                harvest.stop_flag = True
                return self.send_json({"stopping": True})
            if u.path == "/api/refresh":
                if cache.indexing:
                    return self.send_json({"indexing": True})
                threading.Thread(target=cache.refresh, daemon=True).start()
                return self.send_json({"indexing": True})
            return self.send_json({"error": "unknown endpoint"}, 404)

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Diagnostics UI for harvesting every Met department.")
    ap.add_argument("--cache", type=Path, default=Path(os.getenv("MET_OBJ_DIR", "data/met/objects")),
                    help="objects directory (default data/met/objects); state files go next to it")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    cache = Cache(args.cache)
    print(f"indexing {args.cache} in the background; the page is usable while it runs")
    threading.Thread(target=cache.refresh, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(cache, Harvest(cache)))
    print(f"open http://localhost:{args.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
