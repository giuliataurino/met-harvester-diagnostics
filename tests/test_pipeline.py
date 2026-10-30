"""End-to-end tests against a fake Met API (no network)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from met_harvest.client import API, MetClient, NotFound
from met_harvest.fields import DOCUMENTED
from met_harvest.harvest import Harvester
from met_harvest.mapping import aat_resolution, build_rows, staging_sample, write_outputs
from met_harvest.survey import Survey
from met_harvest.cli import main

EXAMPLE = {  # the documented example record, lightly trimmed
    "objectID": 45734, "isHighlight": False, "accessionNumber": "36.100.45", "accessionYear": "1936",
    "isPublicDomain": True, "primaryImage": "https://images.metmuseum.org/CRDImages/as/original/DP251139.jpg",
    "primaryImageSmall": "https://images.metmuseum.org/CRDImages/as/web-large/DP251139.jpg",
    "additionalImages": ["https://images.metmuseum.org/CRDImages/as/original/DP251138.jpg"],
    "constituents": [{"constituentID": 11986, "role": "Artist", "name": "Kiyohara Yukinobu",
                      "constituentULAN_URL": "http://vocab.getty.edu/page/ulan/500034433",
                      "constituentWikidata_URL": "https://www.wikidata.org/wiki/Q11560527", "gender": "Female"}],
    "department": "Asian Art", "objectName": "Hanging scroll", "title": "Quail and Millet", "culture": "Japan",
    "period": "Edo period (1615–1868)", "dynasty": "", "reign": "", "portfolio": "", "artistRole": "Artist",
    "artistPrefix": "", "artistDisplayName": "Kiyohara Yukinobu", "artistDisplayBio": "Japanese, 1643–1682",
    "artistSuffix": "", "artistAlphaSort": "Kiyohara Yukinobu", "artistNationality": "Japanese",
    "artistBeginDate": "1643", "artistEndDate": "1682", "artistGender": "Female",
    "artistWikidata_URL": "https://www.wikidata.org/wiki/Q11560527",
    "artistULAN_URL": "http://vocab.getty.edu/page/ulan/500034433", "objectDate": "late 17th century",
    "objectBeginDate": 1667, "objectEndDate": 1682, "medium": "Hanging scroll; ink and color on silk",
    "dimensions": "46 5/8 x 18 3/4 in. (118.4 x 47.6 cm)",
    "measurements": [{"elementName": "Overall", "elementDescription": None,
                      "elementMeasurements": {"Height": 118.4, "Width": 47.6}}],
    "creditLine": "The Howard Mansfield Collection, Purchase, Rogers Fund, 1936", "geographyType": "", "city": "",
    "state": "", "county": "", "country": "", "region": "", "subregion": "", "locale": "", "locus": "",
    "excavation": "", "river": "", "classification": "Paintings", "rightsAndReproduction": "", "linkResource": "",
    "metadataDate": "2020-09-14T12:26:37.48Z", "repository": "Metropolitan Museum of Art, New York, NY",
    "objectURL": "https://www.metmuseum.org/art/collection/search/45734",
    "tags": [{"term": "Birds", "AAT_URL": "http://vocab.getty.edu/page/aat/300266506",
              "Wikidata_URL": "https://www.wikidata.org/wiki/Q5113"}],
    "objectWikidata_URL": "https://www.wikidata.org/wiki/Q29910832", "isTimelineWork": False, "GalleryNumber": "",
}

DEPTS = [{"departmentId": 6, "displayName": "Asian Art"}, {"departmentId": 13, "displayName": "Greek and Roman Art"}]


def make_record(oid: int, dept: str, **over) -> dict:
    r = dict(EXAMPLE, objectID=oid, department=dept, objectURL=f"https://www.metmuseum.org/art/collection/search/{oid}")
    r.update(over)
    return r


class FakeResponse:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeSession(requests.Session):
    """Two departments; 13 contains an object that claims to be Asian Art, an undocumented key,
    a 404, and a record the model rejects (objectID as string)."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.objects = {
            1: make_record(1, "Asian Art"),
            2: make_record(2, "Asian Art", culture="China", medium="Bronze", additionalImages=[]),
            3: make_record(3, "Greek and Roman Art", culture="Greek, Attic", objectName="Kylix",
                           classification="Vases", medium="Terracotta", objectBeginDate=-500, objectEndDate=-480,
                           surpriseKey="not in docs", dimensionsParsed=[{"element": "Overall", "dimensionType": "Height", "dimension": 10.2}]),
            4: make_record(4, "Asian Art", culture="Roman"),   # listed in 13, says Asian Art
            6: dict(make_record(6, "Greek and Roman Art"), objectID="six"),  # invalid for MetObject
        }
        self.listing = {6: [1, 2], 13: [3, 4, 5, 6]}   # 5 does not exist -> 404

    def get(self, url, params=None, timeout=None, **kw):
        self.calls.append((url, dict(params or {})))
        path = url.replace(API + "/", "")
        if path == "departments":
            return FakeResponse(200, {"departments": DEPTS})
        if path == "objects":
            ids = self.listing.get(int(params["departmentIds"]), [])
            return FakeResponse(200, {"total": len(ids), "objectIDs": ids})
        oid = int(path.split("/")[-1])
        if oid in self.objects:
            return FakeResponse(200, self.objects[oid])
        return FakeResponse(404, {"message": "ObjectID not found"})


@pytest.fixture
def env(tmp_path, monkeypatch):
    sess = FakeSession()
    client = MetClient(tmp_path / "met", rate=1000, session=sess)
    monkeypatch.setattr("met_harvest.cli._client",
                        lambda a: MetClient(a.cache, rate=1000, session=sess, verbose=a.verbose))
    return sess, client, tmp_path / "met"


def test_documented_field_count_matches_api_docs():
    assert len(DOCUMENTED) == 58


def test_harvest_all_departments_resume_and_failures(env):
    sess, client, cache = env
    h = Harvester(client, workers=3, checkpoint=1)
    st = h.run()
    s = h.summary()
    assert s["complete"]
    assert s["totals"] == {"listed": 6, "fetched": 4, "cached": 0, "missing": 1, "failed": 0, "invalid": 1}
    assert (cache / "objects" / "5.missing").exists()
    assert {p.stem for p in (cache / "ids").glob("*.json")} == {"6", "13"}
    fails = [json.loads(l) for l in (cache / "harvest_failures.jsonl").read_text().splitlines()]
    assert [f["objectID"] for f in fails] == [6] and fails[0]["status"] == "invalid"

    # second run: departments are done, nothing refetched
    n_calls = len(sess.calls)
    Harvester(client).run()
    assert len(sess.calls) == n_calls + 1          # only /departments is re-read
    # redo: ids relisted, every object served from cache, 404 marker respected
    Harvester(client).run(redo_done=True)
    obj_calls = [c for c in sess.calls[n_calls:] if "/objects/" in c[0]]
    assert obj_calls == []


def test_check_makes_live_calls_only(env, capsys):
    sess, client, cache = env
    info = client.check(13)
    assert info["api"].startswith("https://collectionapi.metmuseum.org/")
    assert info["departments"] == 2 and info["listed_objects"] == 4 and info["sample_object"]["objectID"] == 3
    assert [c[0].split("/v1/")[1] for c in sess.calls] == ["departments", "objects", "objects/3"]
    assert client.requests_made == 3
    assert main(["--cache", str(cache), "harvest", "--dept", "13"]) == 0   # real CLI path, fake session via env
    assert "live requests this run" in capsys.readouterr().out


def test_client_404_marker_and_cache(env):
    sess, client, cache = env
    with pytest.raises(NotFound):
        client.get_object(5)
    with pytest.raises(NotFound):
        client.get_object(5)
    assert sum("/objects/5" in c[0] for c in sess.calls) == 1
    assert client.get_object(1)["objectID"] == 1
    assert client.get_object(1, refresh=True)["objectID"] == 1
    assert sum("/objects/1" in c[0] for c in sess.calls) == 2


def test_survey_paths_types_departments_mismatch(env):
    sess, client, cache = env
    Harvester(client).run()
    sv = Survey(max_distinct=3)
    sv.load_listings(cache)
    sv.add_all(client.iter_cached())
    res = sv.result()
    assert res["records"] == 5
    paths = res["paths"]
    assert "surpriseKey" in paths and paths["surpriseKey"]["occurrences"] == 1
    assert paths["tags[].term"]["filled"] == 5
    assert paths["measurements[].elementMeasurements.*"]["top_values"][0][0] in ("Height", "Width")
    assert paths["additionalImages"]["empty_lists"] == 1
    assert paths["dynasty"]["empty_strings"] == 5 and paths["dynasty"]["filled"] == 0
    assert paths["objectBeginDate"]["num_min"] == -500
    assert paths["objectID"]["types"] == {"int": 4, "string": 1}
    assert set(res["departments"]) == {"Asian Art", "Greek and Roman Art"}
    assert res["departments"]["Asian Art"]["records"] == 3
    assert res["department_mismatch"] == [{"listed_in": "Greek and Roman Art", "record_says": "Asian Art", "n": 1}]
    # high-cardinality cap
    assert paths["objectURL"]["high_cardinality"] is True
    assert res["object_begin_century"] == {-500: 1, 1600: 4}


def test_mapping_rows_statuses_and_aat(env):
    sess, client, cache = env
    Harvester(client).run()
    sv = Survey(); sv.load_listings(cache); sv.add_all(client.iter_cached())
    res = sv.result()
    rows = build_rows(res)
    by = {r["path"]: r for r in rows}
    assert by["surpriseKey"]["status"] == "undocumented"
    assert by["culture"]["status"] == "documented" and by["culture"]["staging"] == "stg:culture, stg:cultureAAT"
    assert by["tags[].AAT_URL"]["status"] == "documented" and by["tags[]"]["status"] == "documented"
    assert [r["path"] for r in rows if r["status"] == "undocumented"] == ["surpriseKey"]
    assert by["measurements[].elementMeasurements.*"]["status"] == "documented"
    assert by["dimensionsParsed[].dimension"]["status"] == "documented"
    assert by["title"]["fill_rate"] == 1.0
    assert "never_seen" not in {r["status"] for r in rows}  # the example record carries every key
    aat = aat_resolution(res)
    assert aat["culture"]["resolved"] >= 2            # Greek, Roman resolve; Japan/China do not
    assert aat["classification"]["resolved_share"] == 0.8  # Paintings (4) resolves, Vases (1) does not
    assert aat["classification"]["top_unresolved"] == [("Vases", 1)]
    stg = staging_sample(client.iter_cached(), sample=10)
    assert stg["sampled"] == 5 and stg["rejected_by_model"] == 1
    assert stg["triples_per_predicate"]["stg:hasMeasurement"] >= 7   # 4 records x 2 + dimensionsParsed 1
    files = write_outputs(res, cache / "report", stg)
    assert all(f.exists() for f in files)
    report = (cache / "report" / "REPORT.md").read_text()
    assert "`surpriseKey`" in report and "Fill rate by department" in report and "stg:hasMeasurement" in report


def test_cli_end_to_end(env, capsys):
    sess, client, cache = env
    assert main(["--cache", str(cache), "--out", str(cache / "report"), "all", "--dept", "13", "--limit", "2",
                 "--staging-sample", "5"]) == 0
    out = capsys.readouterr().out
    assert '"listed": 2' in out and "REPORT.md" in out
    assert main(["--cache", str(cache), "--out", str(cache / "report"), "map"]) == 0
    assert (cache / "report" / "field_map.csv").exists()
