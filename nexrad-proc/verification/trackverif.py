#!/usr/bin/env python3
"""Track-continuity and envelope-fidelity harness -- the PRIMARY objective.

Runs a contiguous sequence of volumes for each case (a single volume cannot say
anything about tracking) and scores:

  ENVELOPE   convex vs concave vs exact footprint -- IoU against the true
             footprint cells and the excess-area ratio, i.e. how much empty air
             the envelope claims as storm.

  TRACKING   the existing strict-1:1 ``Tracker`` against the split/merge-aware
             ``OverlapTracker``, on Lakshmanan & Smith (2010) duration /
             linearity / attribute-consistency, plus the lifecycle diagnostics
             (orphan births = unrecorded splits, silent deaths = unrecorded
             merges).

The OverlapTracker is scored twice: once on ``track_id`` and once on
``lineage_id``. The lineage figure is the one that matters, because the evidence
chain accumulates against the family, not the post-split id.

Usage:  python trackverif.py [--cases /tmp/cases.json] [--vols 12]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import boto3
import numpy as np
import xradar
from botocore import UNSIGNED
from botocore.config import Config

sys.path.insert(0, os.environ.get("SCIT_SRC", "/opt/nexrad/scit"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scit.envelope import envelope_fidelity, footprint_polygon_xy  # noqa: E402
from scit.grid import grid_datatree                                # noqa: E402
from scit.identify import identify                                 # noqa: E402
from scit.models import GriddedVolume                              # noqa: E402
from scit.params import DetectionParams                            # noqa: E402
from scit.track import Tracker                                     # noqa: E402
from scit.track2 import OverlapTracker, TrackParams                # noqa: E402
from scit.trackmetrics import (TrackSeries, envelope_summary,      # noqa: E402
                               evaluate_tracks, format_report)
from volcache import get_grid                                      # noqa: E402

BUCKET = "unidata-nexrad-level2"
s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED,
                                      max_pool_connections=32),
                  region_name="us-east-1")
TMP = "/mnt/ingest/trackverif.ar2v"


def parse_t(s):
    return datetime.strptime(s, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)


def volume_keys(site, t0, t1):
    keys = []
    day = t0.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= t1:
        tok = None
        while True:
            kw = {"Bucket": BUCKET, "Prefix": f"{day:%Y/%m/%d}/{site}/"}
            if tok:
                kw["ContinuationToken"] = tok
            r = s3.list_objects_v2(**kw)
            for o in r.get("Contents", []):
                k = o["Key"]
                if k.endswith("_MDM"):
                    continue
                b = k.rsplit("/", 1)[-1]
                try:
                    ts = datetime.strptime(b[4:19], "%Y%m%d_%H%M%S").replace(
                        tzinfo=timezone.utc)
                except ValueError:
                    continue
                if t0 <= ts <= t1:
                    keys.append((ts, k))
            tok = r.get("NextContinuationToken")
            if not tok:
                break
        day += timedelta(days=1)
    return sorted(keys)


def load_volume(site, key):
    s3.download_file(BUCKET, key, TMP)
    try:
        dt = xradar.io.open_nexradlevel2_datatree(TMP).load()
        vol, _ = grid_datatree(dt, site)
    finally:
        try:
            os.remove(TMP)
        except OSError:
            pass
    return vol


# ---------------------------------------------------------------- envelope
def score_envelopes(vol, base_params):
    """Fidelity of each envelope method against the same detected footprints."""
    out = {}
    cells = identify(vol, base_params, keep_footprint=True)
    for method in ("convex", "concave", "footprint"):
        recs = []
        for c in cells:
            if c.footprint_mask is None:
                continue
            y0, y1, x0, x1 = c.footprint_bbox
            full = np.zeros((vol.y.size, vol.x.size), bool)
            full[y0:y1 + 1, x0:x1 + 1] = c.footprint_mask
            poly = footprint_polygon_xy(
                vol, full, method=method,
                concave_ratio=base_params.envelope_concave_ratio,
                simplify_frac=base_params.envelope_simplify_frac)
            recs.append(envelope_fidelity(vol, full, poly))
        out[method] = recs
    return out, cells


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="/tmp/cases.json")
    ap.add_argument("--vols", type=int, default=12)
    ap.add_argument("--envelope-every", type=int, default=3,
                    help="score envelope fidelity on every Nth volume")
    args = ap.parse_args()

    P = DetectionParams()
    env_acc = {"convex": [], "concave": [], "footprint": []}
    all_series = []

    for case in json.load(open(args.cases)):
        site = case["sites"][0]
        tag = f"{case['wfo']}-{case['etn']}"
        t0, t1 = parse_t(case["issued"]), parse_t(case["expired"])
        # Widen the window: a warning is ~30-45 min but tracking needs a run.
        vl = volume_keys(site, t0 - timedelta(minutes=30), t1 + timedelta(minutes=30))
        if len(vl) < 3:
            print(f"{tag} {site}: only {len(vl)} volumes -- skipped")
            continue
        # Contiguous block centred on the warning, NOT a scattered sample:
        # tracking metrics are meaningless across a time gap.
        mid = min(range(len(vl)), key=lambda i: abs(
            (vl[i][0] - (t0 + (t1 - t0) / 2)).total_seconds()))
        lo = max(0, mid - args.vols // 2)
        pick = vl[lo:lo + args.vols]

        print(f"\n{'='*70}\n{tag}  {site}  {len(pick)} contiguous volumes "
              f"{pick[0][0]:%H:%M}Z -> {pick[-1][0]:%H:%M}Z\n{'='*70}")

        old_tr = Tracker(P)
        new_tr = OverlapTracker(P, TrackParams())
        ser_old, ser_new = [], []

        for n, (ts, key) in enumerate(pick):
            t_start = time.time()
            try:
                vol, hit = get_grid(site, ts, key, grid_datatree, xradar,
                                    GriddedVolume)
            except Exception as e:
                print(f"  {ts:%H:%M}Z  load failed ({type(e).__name__}) -- gap")
                continue

            if n % args.envelope_every == 0:
                try:
                    envs, cells = score_envelopes(vol, P)
                    for m, r in envs.items():
                        env_acc[m].extend(r)
                except Exception as e:
                    print(f"  {ts:%H:%M}Z  envelope scoring failed "
                          f"({type(e).__name__}: {e})")
                    cells = identify(vol, P)
            else:
                cells = identify(vol, P)

            # Two independent copies -- the trackers mutate track_id in place.
            import copy
            c_old = [copy.copy(c) for c in cells]
            c_new = [copy.copy(c) for c in cells]
            old_tr.update(c_old, ts)
            new_tr.update(c_new, ts)
            ser_old.append(TrackSeries(ts, c_old))
            ser_new.append(TrackSeries(ts, c_new))
            ev = [e.kind for e in new_tr.events if e.t == ts]
            print(f"  {ts:%H:%M}Z  {len(cells):3d} cells  "
                  f"{time.time()-t_start:5.1f}s{'*' if hit else ' '} events: "
                  f"{ {k: ev.count(k) for k in set(ev)} }")

        if len(ser_old) >= 2:
            all_series.append((tag, ser_old, ser_new, new_tr))

    # ------------------------------------------------------------ report
    print(f"\n\n{'#'*70}\n# ENVELOPE FIDELITY  (per detected cell, all cases)\n{'#'*70}")
    print(f"{'method':<12}{'n':>6}{'IoU':>8}{'IoU p05':>9}{'excess':>8}"
          f"{'ex p95':>8}{'ex max':>9}{'false area':>12}")
    for m in ("convex", "concave", "footprint"):
        s = envelope_summary(env_acc[m])
        if not s:
            continue
        print(f"{m:<12}{s['n']:>6}{s['iou_mean']:>8.3f}{s['iou_p05']:>9.3f}"
              f"{s['excess_mean']:>8.2f}{s['excess_p95']:>8.2f}"
              f"{s['excess_max']:>9.2f}{s['false_area_frac']:>11.1%}")
    print("  IoU 1.0 = wraps exactly the cells it claims; excess 1.0 = no empty air.")

    print(f"\n\n{'#'*70}\n# TRACK CONTINUITY  (Lakshmanan & Smith 2010)\n{'#'*70}")
    for tag, so, sn, tr in all_series:
        print(f"\n### {tag}")
        print(format_report("Tracker (strict 1:1, current)",
                            evaluate_tracks(so)))
        print(format_report("OverlapTracker by track_id",
                            evaluate_tracks(sn)))
        print(format_report("OverlapTracker by lineage_id",
                            evaluate_tracks(sn, id_attr="lineage_id")))
        kinds = {}
        for e in tr.events:
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        print(f"  explicit lifecycle events: {kinds}")

    if len(all_series) > 1:
        print(f"\n\n{'#'*70}\n# POOLED ACROSS CASES\n{'#'*70}")
        pool_old = [s for _, so, _, _ in all_series for s in so]
        pool_new = [s for _, _, sn, _ in all_series for s in sn]
        # Pool per case, then average -- concatenating series across cases would
        # invent time gaps and fabricate deaths and births at every boundary.
        def _avg(sers, **kw):
            ms = [evaluate_tracks(s, **kw) for s in sers]
            keys = [k for k in ms[0] if isinstance(ms[0][k], (int, float))]
            return {k: float(np.nanmean([m[k] for m in ms])) for k in keys}
        print(format_report("Tracker (strict 1:1, current)",
                            _avg([so for _, so, _, _ in all_series])))
        print(format_report("OverlapTracker by track_id",
                            _avg([sn for _, _, sn, _ in all_series])))
        print(format_report("OverlapTracker by lineage_id",
                            _avg([sn for _, _, sn, _ in all_series],
                                 id_attr="lineage_id")))


if __name__ == "__main__":
    main()
