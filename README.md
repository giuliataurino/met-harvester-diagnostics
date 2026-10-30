# Met harvester diagnostics

A local web UI for checking the paa_graph_kb Met harvester against what The Met Collection Public
API returns for **every curatorial department**. No dependency on `paa_graph_kb`.

This is [met-harvester-diagnostics-department13](https://github.com/giuliataurino/met-harvester-diagnostics-department13)
scaled from Greek and Roman Art (department 13) to the whole collection. Same five tabs, same
`<objectID>.json` cache layout, same copied model / staging / AAT code; what changed is listed under
[What is different from the department-13 tool](#what-is-different-from-the-department-13-tool).

## Setup

```bash
git clone https://github.com/giuliataurino/met-harvester-diagnostics.git
cd met-harvester-diagnostics
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt   # requests, pydantic, rdflib; pytest
python -m pytest diagnostics/test_server.py               # 3 passed, offline

python diagnostics/server.py --cache data/met/objects
```

Open <http://localhost:8765>. Use `--port 8080` if 8765 is taken (for instance by the department-13
tool, which also defaults to 8765; both can run at once on different ports). Records are cached as
`data/met/objects/<objectID>.json`; `data/` is ignored by git.

Every request goes to `https://collectionapi.metmuseum.org/public/collection/v1`; nothing else is read.

## What it shows

**Harvest** — one row per department from `/departments`, with a checkbox. Tick the ones to fetch
(none ticked means all), set an optional limit per department, the number of workers and the
request rate, and Start. Each row shows listed / done / fetched / from cache / missing / failed /
model-rejected and a progress bar; the totals above cover the run. Pause, Stop, Re-index cache.
Cached records are read from disk; only new ids hit the API, through one rate limiter shared by all
workers (default 20 requests/s; the Met asks for at most 80). The whole collection is about 480,000
objects, roughly seven hours at 20 req/s, and it is safe to Stop and Start again: cached ids cost no
request, 404s are marked `<objectID>.missing` and never retried, and a department finished on an
earlier run is skipped unless *redo finished departments* is ticked. Failed ids are listed with
their error, including any record the model refuses, and logged to `data/met/harvest_failures.jsonl`.

**Objects** — browse the cache as an image grid; search title, object name, culture, medium, artist,
accession number, period, classification, department or id; filter to one department; filter to
objects where a chosen field is empty. Open one to see the raw API record next to the Turtle it
stages to, a presence strip for all 58 documented fields, any keys the API served that the schema
does not document, the AAT lookups made for culture, object name, classification and medium, and
counts of image, measurement, person and subject nodes.

**Coverage** — for one department or the whole cache: how often each field is filled, which
undocumented keys appear, the share of objects whose terms resolve to an AAT URI, and the most
frequent terms that don't. Below it, a department comparison: a heatmap of fill rate for every
documented field in every department, and per department the mean fill, fields never filled,
fields always filled, undocumented keys, and AAT resolution for the four looked-up fields. This is
where schema drift and the departments the mapping tables do not cover show up.

**Perseus links** — point it at a Perseus staging TTL to see how many Perseus objects held by the
Met match an accession number in the cache (whitespace and case ignored), which don't, and which
departments the matches fall in.

**Live API** — call the Met directly for any id, bypassing the cache, and compare: cache vs live
(stale copies, museum edits), live vs model (unknown keys, values changed by validation), model vs
staging (filled fields that produced no triple).

## What is different from the department-13 tool

| | department-13 | this repo |
|---|---|---|
| Scope | one department, typed as a number | any set of departments, picked from the live `/departments` list; none = all |
| Workers | one thread | 1–16 threads behind one shared rate limiter |
| 404s | counted as failed, retried every run | written as `<objectID>.missing`, never retried |
| Resume | rerun re-reads everything | `data/met/harvest_state.json` records finished departments; they are skipped unless redone |
| Failures | first 200 shown in the page | same, plus every one appended to `data/met/harvest_failures.jsonl` |
| Id listings | not kept | saved to `data/met/ids/<departmentId>.json` so the id universe of a run is on disk |
| Index | whole records in memory | a small summary per object (grid fields, a 58-bit presence mask, AAT terms, search text); the full record is read from disk when opened, so ~480k objects fit |
| Indexing | blocks startup | runs in a background thread; the page opens at once and shows progress |
| Coverage | one total | kept per department as records arrive, plus the field × department matrix |
| Package import | needed a `metdiag` symlink | `server.py` puts the repo root on `sys.path`; no symlink |

The cache layout is unchanged, so this tool and the department-13 one can be pointed at the same
`data/met/objects` directory. Harvesting is one-way, though: harvest here and inspect there is fine,
but the department-13 tool does not know about `.missing` markers and will retry those ids.

## Files

```
diagnostics/
  server.py        HTTP server: cache index, harvest, diagnostics endpoints
  index.html       the page
  test_server.py   offline test: fake API, two departments, every endpoint
metdiag/
  client.py        API client, cache, shared rate limiter, 404 markers, department and id listings
  models.py        copy of paa_graph_kb/models/met/models.py
  staging.py       copy of met_object_to_staging() from paa_graph_kb/harvesters/met_harvester.py
  aat.py           copy of paa_graph_kb/vocabularies/aat_mappings.py (verbatim)
requirements.txt, requirements-dev.txt, LICENSE (GPL-3.0), .gitignore
```

`models.py`, `staging.py` and `aat.py` are the same copies the department-13 tool carries. The cost
of standing alone is drift: if paa_graph_kb changes its mapping or AAT tables, this tool keeps showing
the old behaviour until the copies are refreshed. Each file says where it came from.

The cache layout also differs from paa_graph_kb: its base client names files by request hash; this
tool uses `<objectID>.json`. The Objects and Coverage views read either, since they identify records
by content, so `--cache /path/to/paa_graph_kb/data/met/objects` works for inspection. Harvesting
through this tool writes `<objectID>.json` files, which the repo's client will not recognise as cached.

## Test

```bash
python -m pytest diagnostics/test_server.py
```

Offline: the Met API is mocked with two departments, one cached record, one new record and one 404,
and the test runs a full harvest, a resumed harvest, a redo with a limit, a failing listing, and
every read endpoint.
