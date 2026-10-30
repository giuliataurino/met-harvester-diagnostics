# met_harvest — all-department Met harvester with metadata survey

Harvests every object in every curatorial department from The Met Collection Public API,
then surveys the metadata (every JSON path actually served, its types, fill rates, values,
and how these vary by department) and maps each path to the documented schema and to the
`stg:` staging predicates used by `paa_graph_kb`.

It generalises [met_harvester_diagnostics](https://github.com/giuliataurino/met_harvester_diagnostics)
from Greek and Roman Art (department 13) to the whole collection, and keeps its
`<objectID>.json` cache layout so the diagnostics UI can be pointed at the same cache
(`python server.py --cache data/met/objects`).

## Setup

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt            # requests, pydantic, rdflib
pip install -r requirements-dev.txt && pytest   # optional
```

## Usage

```
python -m met_harvest check                                # 3 live calls to collectionapi.metmuseum.org, URLs printed
python -m met_harvest departments                          # list department ids and names
python -m met_harvest harvest --dept 13 --limit 200        # trial run
python -m met_harvest harvest --workers 4 --rate 20        # everything (~480k objects)
python -m met_harvest survey --staging-sample 2000         # read the cache, write survey.json
python -m met_harvest map                                  # field_map.csv/json + REPORT.md
python -m met_harvest all                                  # the three steps in one command
```

Add `--verbose` before the subcommand to log every request URL and status. Every request goes
to `https://collectionapi.metmuseum.org/public/collection/v1`; nothing else is read.

Default cache root is `data/met`, report dir `data/met/report`; both take `--cache` / `--out`.

### Harvest behaviour

- `/departments` is read on every run; `/objects?departmentIds=N` is read per department
  and saved to `ids/<N>.json`, so the id universe of a run is on disk. `/v1.1/search` is not
  used for enumeration: it is capped at 10,000 results.
- Each object is fetched once and stored at `objects/<objectID>.json`, byte-for-byte as served.
  A 404 leaves `objects/<objectID>.missing` so it is not retried. `--refresh` refetches,
  `--since YYYY-MM-DD` passes `metadataDate` to pick up only updated objects.
- Workers share one rate limiter (default 20 req/s; the Met asks for at most 80). At 20 req/s
  the full collection takes about seven hours; it is safe to Ctrl-C and rerun, since cached
  ids cost no request. Progress is checkpointed to `harvest_state.json`; failures and
  records the `MetObject` model rejects go to `harvest_failures.jsonl` with the reason.
- A department is skipped on rerun once marked done, unless `--redo`, `--refresh` or `--since`.

### Survey

`survey.json` holds, for every JSON path seen (`culture`, `tags[].term`,
`measurements[].elementMeasurements.*`, …): occurrences, non-empty fills, nulls, empty strings
and lists, observed types, distinct-value count, top values, numeric and string-length ranges,
and three example values with their object ids. Value counting stops per path after
`--max-distinct` values (default 5,000) so memory stays flat over the full collection; the
path is then flagged high-cardinality. It also records, per department, the fill rate of every
top-level field; objects whose own `department` disagrees with the listing they came from;
accession numbers shared by several objects; and a century histogram of `objectBeginDate`.

### Field map

`field_map.csv` / `.json` has one row per path: status (`documented`, `undocumented`,
`never_seen`), documented type vs observed types, fill rate, distinct count, top values,
the staging predicates it feeds and the AAT table it is looked up in. `REPORT.md` renders the
same with the per-department fill matrix, AAT resolution rates and the most frequent terms
that do not resolve, and (with `--staging-sample`) triples per predicate plus fields that were
filled in the raw record but produced none of their expected triples.

The reference side lives in `met_harvest/fields.py` (58 fields, from
https://metmuseum.github.io/#object) and should be updated when the Met documents new fields;
anything the API serves that is not there shows up as `undocumented` in the report, which is
how schema drift surfaces.

## Files copied from met_harvester_diagnostics

`models.py`, `staging.py` and `aat.py` are copies (the first two originally from
`paa_graph_kb`). As with the diagnostics tool, they drift if the upstream mapping changes;
each file says where it came from. The staging code is only used by the `--staging-sample`
check; the harvest and survey do not depend on it.

## Layout

```
met_harvest/
  client.py    API client, cache, rate limiter
  harvest.py   all-department orchestration, resume, failure log
  survey.py    path/type/value/department statistics
  fields.py    documented schema + staging map (reference side)
  mapping.py   joins survey with fields.py; writes CSV / JSON / REPORT.md
  cli.py       command line
  models.py, staging.py, aat.py   copied, see above
tests/test_pipeline.py   runs the whole pipeline against a fake API
```

License: GPL-3.0, as the diagnostics repo.
