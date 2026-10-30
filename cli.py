"""met-harvest: harvest every Met department from the public API, survey the metadata, map it.

    python -m met_harvest check     (three live calls, URLs printed)
    python -m met_harvest departments
    python -m met_harvest harvest   [--dept 13 --dept 10] [--limit 200] [--workers 4] [--rate 20]
    python -m met_harvest survey    [--staging-sample 1000]
    python -m met_harvest map
    python -m met_harvest all       (harvest + survey + map)
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

from .client import API, MetClient
from .harvest import Harvester
from .mapping import staging_sample, write_outputs
from .survey import Survey


def _client(a) -> MetClient:
    return MetClient(a.cache, rate=a.rate, verbose=a.verbose)


def cmd_check(a) -> int:
    """Prove connectivity: three uncached live calls, URLs printed."""
    client = MetClient(a.cache, rate=a.rate, verbose=True)
    try:
        info = client.check(a.dept)
    except Exception as e:  # noqa: BLE001
        print(f"cannot reach {API}: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1
    print(json.dumps(info, indent=1, ensure_ascii=False))
    return 0


def cmd_departments(a) -> int:
    for d in _client(a).departments():
        n = len(_client(a).object_ids(d.id, refresh=False)) if a.counts else None
        print(f"{d.id:>3}  {d.name}" + (f"  ({n:,} ids cached)" if n is not None else ""))
    return 0


def cmd_harvest(a) -> int:
    client = _client(a)
    last = {"t": 0.0}

    def progress(prog, done, total):
        now = time.time()
        if now - last["t"] >= 2 or done == total:
            last["t"] = now
            print(f"\r[{prog.department_id:>3} {prog.name[:34]:<34}] {done:>7,}/{total:<7,} "
                  f"fetched {prog.fetched:,} cached {prog.cached:,} missing {prog.missing} "
                  f"failed {prog.failed} invalid {prog.invalid}", end="", file=sys.stderr, flush=True)
            if done == total:
                print(file=sys.stderr)

    h = Harvester(client, workers=a.workers, checkpoint=a.checkpoint, validate=not a.no_validate,
                  on_progress=progress)
    signal.signal(signal.SIGINT, lambda *_: (print("\nstopping after in-flight requests…", file=sys.stderr),
                                             h.stop.set()))
    h.run(only=a.dept or None, limit_per_dept=a.limit, since=a.since,
          refresh_ids=not a.keep_ids, refresh_objects=a.refresh, redo_done=a.redo)
    s = h.summary()
    print(json.dumps(s["totals"], indent=1))
    print(f"complete: {s['complete']}  live requests this run: {client.requests_made:,}  "
          f"state: {h.state_path}  failures: {h.failures_path}")
    return 0 if s["complete"] or h.stop.is_set() else 1


def _run_survey(a, client: MetClient) -> tuple[dict, dict | None]:
    sv = Survey(max_distinct=a.max_distinct, examples=a.examples, top_values=a.top_values)
    sv.load_listings(client.cache_dir)
    t0 = time.time()
    n = 0
    for rec in client.iter_cached():
        sv.add(rec)
        n += 1
        if n % 20000 == 0:
            print(f"\rsurveyed {n:,} records", end="", file=sys.stderr, flush=True)
    print(f"\rsurveyed {n:,} records in {time.time()-t0:.0f}s, {len(sv.paths)} paths", file=sys.stderr)
    out = Path(a.out)
    res = sv.save(out / "survey.json")
    stg = None
    if a.staging_sample:
        stg = staging_sample(client.iter_cached(), sample=a.staging_sample)
        (out / "staging_sample.json").write_text(json.dumps(stg, indent=1), encoding="utf-8")
    return res, stg


def cmd_survey(a) -> int:
    _run_survey(a, _client(a))
    print(f"wrote {Path(a.out)/'survey.json'}")
    return 0


def cmd_map(a) -> int:
    out = Path(a.out)
    sp = out / "survey.json"
    if not sp.exists():
        print(f"{sp} not found; run `survey` first", file=sys.stderr)
        return 1
    survey = json.loads(sp.read_text(encoding="utf-8"))
    stg_p = out / "staging_sample.json"
    stg = json.loads(stg_p.read_text(encoding="utf-8")) if stg_p.exists() else None
    for p in write_outputs(survey, out, stg):
        print(f"wrote {p}")
    return 0


def cmd_all(a) -> int:
    rc = cmd_harvest(a)
    if rc and not a.continue_on_failure:
        return rc
    res, stg = _run_survey(a, _client(a))
    for p in write_outputs(res, Path(a.out), stg):
        print(f"wrote {p}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="met-harvest", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default="data/met", help="cache root (default data/met)")
    ap.add_argument("--out", default="data/met/report", help="survey / map output dir")
    ap.add_argument("--rate", type=float, default=20, help="requests per second, shared by all workers (max 80)")
    ap.add_argument("--verbose", action="store_true", help="log every API request URL and status to stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="make three live calls to collectionapi.metmuseum.org and print them")
    p.add_argument("--dept", type=int, default=13)
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("departments", help="list departments")
    p.add_argument("--counts", action="store_true", help="show cached id counts")
    p.set_defaults(fn=cmd_departments)

    def harvest_args(p):
        p.add_argument("--dept", type=int, action="append", help="department id (repeatable); default all")
        p.add_argument("--limit", type=int, help="max objects per department (for trials)")
        p.add_argument("--since", help="metadataDate YYYY-MM-DD: only objects updated after this date")
        p.add_argument("--workers", type=int, default=4)
        p.add_argument("--checkpoint", type=int, default=500)
        p.add_argument("--keep-ids", action="store_true", help="reuse cached id listings instead of refetching")
        p.add_argument("--refresh", action="store_true", help="refetch objects already cached")
        p.add_argument("--redo", action="store_true", help="revisit departments marked done")
        p.add_argument("--no-validate", action="store_true", help="skip MetObject validation while harvesting")

    def survey_args(p):
        p.add_argument("--max-distinct", type=int, default=5000)
        p.add_argument("--examples", type=int, default=3)
        p.add_argument("--top-values", type=int, default=25)
        p.add_argument("--staging-sample", type=int, default=1000,
                       help="records to run through the staging mapping (0 to skip)")

    p = sub.add_parser("harvest", help="fetch id listings and objects for all (or chosen) departments")
    harvest_args(p); p.set_defaults(fn=cmd_harvest)
    p = sub.add_parser("survey", help="survey every cached record")
    survey_args(p); p.set_defaults(fn=cmd_survey)
    p = sub.add_parser("map", help="write field map + report from survey.json")
    p.set_defaults(fn=cmd_map)
    p = sub.add_parser("all", help="harvest, survey and map in one go")
    harvest_args(p); survey_args(p)
    p.add_argument("--continue-on-failure", action="store_true")
    p.set_defaults(fn=cmd_all)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
