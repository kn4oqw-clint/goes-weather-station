#!/usr/bin/env python3
"""Sweep the family grouping distance against the same volumes.

`merge_split_MEST(distance=...)` decides how aggressively linked cells are
grouped into a storm complex. Too coarse and the whole domain becomes one
family; too fine and a squall line fragments and stops being trackable as a
single entity. There is a real optimum and it is measurable.

Volumes are gridded ONCE and detection is run ONCE per volume -- only the
grouping is swept -- so the sweep costs one pass of the expensive work rather
than one per setting.

Reported per setting:
  families/volume     -- 1 for a whole squall line is intended; 40 is not
  longest family      -- can a complex hold one identity for its lifetime?
  envelope continuity -- volume-to-volume IoU of a family's own footprint,
                         the right continuity measure for something that
                         legitimately absorbs neighbours
  membership churn    -- expected to be high; reported, not optimised
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# SCIT_SRC LAST so it lands at sys.path[0] and wins. The script dir
# also contains a scit/ tree, and inserting it afterwards would shadow
# SCIT_SRC entirely -- which silently tested the wrong copy once.
sys.path.insert(0, os.environ.get("SCIT_SRC", "/app"))
warnings.filterwarnings("ignore")

import xradar                                                     # noqa: E402
from scit.grid import grid_datatree                               # noqa: E402
from scit.hierarchy import HierarchyParams, StormHierarchy        # noqa: E402
from scit.models import GriddedVolume                             # noqa: E402
from scit.params import DetectionParams                           # noqa: E402
from scit.tobac_detect import detect_volume                       # noqa: E402
from volcache import get_grid, volume_keys                        # noqa: E402

P = DetectionParams()


def parse_t(s):
    return datetime.strptime(s, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="/tmp/cases.json")
    ap.add_argument("--vols", type=int, default=8)
    ap.add_argument("--distances", default="10,25,50,100,200")
    args = ap.parse_args()
    dists = [float(x) for x in args.distances.split(",")]

    for case in json.load(open(args.cases)):
        site = case["sites"][0]
        tag = f"{case['wfo']}-{case['etn']}"
        t0, t1 = parse_t(case["issued"]), parse_t(case["expired"])
        vl = volume_keys(site, t0 - timedelta(minutes=30), t1 + timedelta(minutes=30))
        if len(vl) < 3:
            continue
        mid = min(range(len(vl)), key=lambda i: abs(
            (vl[i][0] - (t0 + (t1 - t0) / 2)).total_seconds()))
        pick = vl[max(0, mid - args.vols // 2):][:args.vols]

        # Grid + detect once; the sweep only varies the grouping.
        prepared = []
        for ts, key in pick:
            try:
                vol, _ = get_grid(site, ts, key, grid_datatree, xradar,
                                  GriddedVolume)
                dets, feats = detect_volume(vol, P)
            except Exception as e:
                print(f"  {ts:%H:%M}Z skip ({type(e).__name__})")
                continue
            prepared.append((vol, dets, feats))
        if len(prepared) < 3:
            print(f"{tag}: too few usable volumes")
            continue

        print(f"\n{'='*78}\n{tag} {site}  {len(prepared)} volumes, "
              f"{np.mean([len(d) for _, d, _ in prepared]):.1f} cells/volume\n{'='*78}")
        print(f"{'dist km':>8}{'fam/vol':>9}{'families':>10}{'longest min':>13}"
              f"{'env IoU':>9}{'churn':>8}{'max cells/fam':>15}")

        for dkm in dists:
            H = StormHierarchy(site, P, HierarchyParams(
                family_distance_m=dkm * 1000.0))
            counts, overlaps, biggest = [], [], 0
            prev_env, prev_mem = {}, {}
            churn_n = churn_d = 0
            for vol, dets, feats in prepared:
                _, fams = H.update(vol, dets, feats)
                counts.append(len(fams))
                for f in fams:
                    biggest = max(biggest, f["n_cells"])
                    pe = prev_env.get(f["family_uid"])
                    ce = f["envelope_xy"]
                    if pe is not None and not pe.is_empty and not ce.is_empty:
                        inter = pe.intersection(ce).area
                        uni = pe.area + ce.area - inter
                        if uni > 0:
                            overlaps.append(inter / uni)
                    prev_env[f["family_uid"]] = ce
                    cur = set(f["cell_uids"])
                    old = prev_mem.get(f["family_uid"])
                    if old:
                        churn_n += len(cur ^ old)
                        churn_d += len(cur | old)
                    prev_mem[f["family_uid"]] = cur
            longest = max((n.duration_min for n in H.families.values()),
                          default=0.0)
            print(f"{dkm:>8.0f}{np.mean(counts):>9.1f}{len(H.families):>10}"
                  f"{longest:>13.0f}"
                  f"{(np.mean(overlaps) if overlaps else float('nan')):>9.3f}"
                  f"{(churn_n/churn_d if churn_d else float('nan')):>8.3f}"
                  f"{biggest:>15}")


if __name__ == "__main__":
    main()
