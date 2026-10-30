"""Offline test: fake cache + mocked MetClient, exercises every endpoint across two departments."""
import json, threading, time, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

import server

SAMPLE = {
    "objectID": 254904, "accessionNumber": "56.171.49", "accessionYear": "1956", "isPublicDomain": True,
    "department": "Greek and Roman Art", "objectName": "Kylix", "title": "Terracotta kylix", "culture": "Greek, Attic",
    "period": "Archaic", "objectDate": "ca. 520 BCE", "objectBeginDate": -530, "objectEndDate": -510, "medium": "Terracotta",
    "classification": "Vases", "creditLine": "Fletcher Fund, 1956", "country": "", "primaryImageSmall": "https://example.invalid/x.jpg",
    "additionalImages": [], "constituents": [], "tags": [{"term": "Dionysus", "AAT_URL": "http://vocab.getty.edu/page/aat/300343813"}],
    "measurements": [{"elementName": "Overall", "elementMeasurements": {"Height": 10.8}}], "objectURL": "https://www.metmuseum.org/art/collection/search/254904",
}
EGYPT = dict(SAMPLE, objectID=544320, department="Egyptian Art", objectName="Scarab", title="Scarab of Thutmose",
             culture="", medium="Steatite", classification="", period="New Kingdom", accessionNumber="26.7.1",
             newKey="served but undocumented")
DEPARTMENTS = [{"departmentId": 10, "displayName": "Egyptian Art"}, {"departmentId": 13, "displayName": "Greek and Roman Art"}]
LISTINGS = {10: [544320, 544321], 13: [254904, 2, 7]}


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as r:
        return json.loads(r.read()) if "json" in r.headers.get("Content-Type", "") else r.read().decode()


def post(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def fake_get(self, path, params=None):
    if path == "departments":
        return {"departments": DEPARTMENTS}
    if path == "objects":
        return {"objectIDs": LISTINGS[params["departmentIds"]]}
    oid = int(path.rsplit("/", 1)[1])
    if oid == 7:
        resp = requests.Response(); resp.status_code = 404
        raise requests.HTTPError(response=resp)
    return dict(EGYPT if oid in LISTINGS[10] else SAMPLE, objectID=oid)


@pytest.fixture
def srv(tmp_path: Path):
    cache_dir = tmp_path / "met" / "objects"; cache_dir.mkdir(parents=True)
    (cache_dir / "deadbeef.json").write_text(json.dumps(SAMPLE))        # hash-named file, paa_graph_kb layout
    (cache_dir / "544320.json").write_text(json.dumps(EGYPT))
    cache = server.Cache(cache_dir); assert cache.refresh() == 2
    harvest = server.Harvest(cache)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(cache, harvest))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    with patch.object(server.MetClient, "_get", fake_get):
        yield httpd.server_address[1], cache_dir, cache
    httpd.shutdown()


def wait_idle(port):
    for _ in range(100):
        s = get(port, "/api/status")
        if not s["running"]:
            return s
        time.sleep(0.1)
    raise AssertionError("harvest did not finish")


def test_index_objects_and_coverage(srv, tmp_path):
    port, cache_dir, cache = srv
    assert "<title>Met harvest diagnostics" in get(port, "/")
    s = get(port, "/api/status")
    assert s["in_cache"] == 2 and {d["department"] for d in s["cache_departments"]} == {"Greek and Roman Art", "Egyptian Art"}

    assert get(port, "/api/objects?q=kylix")["total"] == 1
    assert get(port, "/api/objects?department=Egyptian+Art")["total"] == 1
    assert get(port, "/api/objects?missing=country")["total"] == 2
    assert get(port, "/api/objects?missing=culture")["total"] == 1          # Egyptian record has culture ""
    assert get(port, "/api/objects?missing=title")["total"] == 0

    d = get(port, "/api/object/254904")
    assert d["triples"] > 20 and "stg:objectnumber" in d["turtle"] and d["nodes"]["measurements"] == 1
    aat = {a["field"]: bool(a["uri"]) for a in d["aat"]}
    assert aat["culture"] and aat["medium"]            # "Greek" and "Terracotta" are in aat_mappings
    assert get(port, "/api/object/544320")["undocumented"] == ["newKey"]

    cov = get(port, "/api/coverage")
    assert cov["objects"] == 2 and cov["fields"]["title"] == 2 and cov["fields"]["culture"] == 1
    assert cov["undocumented"] == {"newKey": 1}
    cov13 = get(port, "/api/coverage?department=Greek+and+Roman+Art")
    assert cov13["objects"] == 1 and cov13["aat"]["culture"]["mapped_objects"] == 1
    m = get(port, "/api/coverage/matrix")
    assert len(m["departments"]) == 2 and len(m["fields"]) == 58
    egypt = next(r for r in m["departments"] if r["department"] == "Egyptian Art")
    assert "culture" in egypt["never_filled"] and "title" in egypt["always_filled"] and egypt["undocumented"] == ["newKey"]

    perseus = tmp_path / "perseus.ttl"
    perseus.write_text('@prefix stg: <https://aa.perseus.org/staging#> .\n'
                       '<urn:a> stg:institution "Metropolitan Museum of Art" ; stg:accessionNumber "56.171.49" .\n'
                       '<urn:b> stg:institution "Metropolitan Museum of Art" ; stg:accessionNumber "99.1.1" .\n')
    p = get(port, f"/api/perseus?path={perseus}")
    assert p["matched"] == 1 and p["unmatched"] == ["99.1.1"] and p["by_department"] == {"Greek and Roman Art": 1}


def test_harvest_all_departments_resumes(srv):
    port, cache_dir, cache = srv
    deps = get(port, "/api/departments")["departments"]
    assert [d["departmentId"] for d in deps] == [10, 13] and deps[0]["in_cache"] == 1 and deps[0]["done"] is None

    # everything (no departments= means all): 5 ids over two departments, one already cached, one 404
    assert post(port, "/api/harvest?workers=2&rate=50")["started"]
    s = wait_idle(port)
    assert (s["total"], s["done"], s["fetched"], s["cached"], s["missing"], s["failed"]) == (5, 5, 3, 1, 1, 0)
    rows = {r["departmentId"]: r for r in s["departments"]}
    assert rows[10]["status"] == "done" and rows[13]["status"] == "done"
    assert rows[13]["missing"] == 1 and rows[10]["fetched"] == 1 and rows[10]["cached"] == 1
    assert (cache_dir / "2.json").exists() and (cache_dir / "7.missing").exists()
    assert get(port, "/api/objects")["total"] == 4
    assert get(port, "/api/objects?department=Egyptian+Art")["total"] == 2

    state = json.loads((cache_dir.parent / "harvest_state.json").read_text())
    assert set(state["done"]) == {"10", "13"}

    # second run: both departments are skipped, nothing is requested, and no .missing id is retried
    assert post(port, "/api/harvest")["started"]
    s = wait_idle(port)
    assert s["total"] == 0 and all("skipped" in r["status"] for r in s["departments"])

    # redo one department with a limit: the limit keeps it from being marked done again
    (cache_dir.parent / "harvest_state.json").unlink()
    assert post(port, "/api/harvest?departments=13&limit=2&redo=1")["started"]
    s = wait_idle(port)
    assert s["total"] == 2 and s["cached"] == 2 and [r["departmentId"] for r in s["departments"]] == [13]
    assert not (cache_dir.parent / "harvest_state.json").exists()

    # a department whose listing fails is reported and the others still run
    def failing(self, path, params=None):
        if path == "objects" and params["departmentIds"] == 10:
            raise requests.ConnectionError("boom")
        return fake_get(self, path, params)
    with patch.object(server.MetClient, "_get", failing):
        assert post(port, "/api/harvest?redo=1")["started"]
        s = wait_idle(port)
    rows = {r["departmentId"]: r for r in s["departments"]}
    assert rows[10]["status"].startswith("listing failed") and rows[13]["status"] == "done"


def test_live_compare(srv):
    port, cache_dir, cache = srv
    live = dict(SAMPLE, creditLine="Fletcher Fund, 1956 (updated)", newKey="x", metadataDate="2026-10-01T00:00:00Z")

    class R:
        status_code = 200
        def json(self): return live
    with patch.object(server.requests, "get", lambda *a, **k: R()):
        d = get(port, "/api/live/254904")
    assert d["status"] == 200
    assert {x["field"] for x in d["cache_diff"]} == {"creditLine", "newKey", "metadataDate"}
    assert d["model_unknown_keys"] == ["newKey"]
    assert {x["field"] for x in d["model_changed"]} == set()   # "1956" -> 1956 counts as equivalent
    assert d["unstaged"] == [] and d["triples"] > 20


def test_refresh_relies_on_live_api(srv):
    port, cache_dir, cache = srv
    # stale cached copy of 254904 and a .missing marker for 2; the live API now serves both
    (cache_dir / "deadbeef.json").unlink()
    (cache_dir / "254904.json").write_text(json.dumps(dict(SAMPLE, title="OLD TITLE")))
    (cache_dir / "2.missing").touch()
    cache.refresh()
    assert get(port, "/api/objects?q=old+title")["total"] == 1

    assert post(port, "/api/harvest?departments=13&refresh=1")["started"]
    s = wait_idle(port)
    # every id went to the API: nothing read from cache, the 404 marker for 2 was retried and cleared
    assert (s["total"], s["fetched"], s["cached"], s["missing"]) == (3, 2, 0, 1) and s["refresh"]
    assert get(port, "/api/objects?q=old+title")["total"] == 0          # overwritten with the live record
    assert get(port, "/api/objects?q=kylix")["total"] == 2
    assert not (cache_dir / "2.missing").exists() and (cache_dir / "7.missing").exists()

    # a record the live API stopped serving loses its stale cached copy on refresh
    (cache_dir / "7.json").write_text(json.dumps(dict(SAMPLE, objectID=7)))
    assert post(port, "/api/harvest?departments=13&refresh=1")["started"]
    wait_idle(port)
    assert not (cache_dir / "7.json").exists()

    # updated-since: metadataDate goes to the listing call, the result is refetched, nothing is marked done
    seen = {}
    def since_get(self, path, params=None):
        if path == "objects":
            seen.update(params); return {"objectIDs": [254904]}
        return fake_get(self, path, params)
    (cache_dir.parent / "harvest_state.json").unlink(missing_ok=True)
    with patch.object(server.MetClient, "_get", since_get):
        assert post(port, "/api/harvest?departments=13&since=2026-10-01")["started"]
        s = wait_idle(port)
    assert seen["metadataDate"] == "2026-10-01" and (s["total"], s["fetched"], s["cached"]) == (1, 1, 0)
    assert not (cache_dir.parent / "harvest_state.json").exists()

    with pytest.raises(urllib.error.HTTPError):
        post(port, "/api/harvest?since=yesterday")
