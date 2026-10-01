#!/usr/bin/env python3
"""Validate the two-level hierarchy in STREAMING mode.

The point of this harness is that it feeds volumes one at a time, exactly as the
service does, and checks the thing batch tobac cannot tell us: whether the
rolling-window re-link produces *stable* identities. A batch run always looks
clean because it sees the whole time series at once.

Checks:
  * cell and family identity persistence (the L&S-2010 suite, streaming)
  * whether family membership churns volume to volume
  * that a family envelope wraps its members (union of footprints, not a hull)
  * the growth trends that the comparator will consume
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone

import numpy as np

# tobac warns on every single-time segmentation call; one volume at a time is
# exactly how this pipeline runs, so the warning is noise, not a signal.
warnings.filterwarnings("ignore", category=UserWarning, module="tobac.*")

sys.path.insert(0, "/tmp/pkgs")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# SCIT_SRC LAST so it lands at sys.path[0] and wins. The script dir
# also contains a scit/ tree, and inserting it afterwards would shadow
# SCIT_SRC entirely -- which silently tested the wrong copy once.
sys.path.insert(0, os.environ.get("SCIT_SRC", "/app"))

import xradar                                                      # noqa: E402
from scit.envelope import envelope_fidelity                        # noqa: E402
from scit.grid import grid_datatree                                # noqa: E402
from scit.hierarchy import HierarchyParams, StormHierarchy         # noqa: E402
from scit.models import GriddedVolume                              # noqa: E402
from scit.params import DetectionParams                            # noqa: E402
from scit.tobac_detect import detect_volume                        # noqa: E402
from scit.trackmetrics import (TrackSeries, evaluate_tracks,       # noqa: E402
                               format_report)
from volcache import get_grid, volume_keys                         # noqa: E402

P = DetectionParams()


class Cell:
    """Adapter so scit.trackmetrics can score hierarchy output unchanged."""
    __slots__ = ("seed_x", "seed_y", "area_km2", "max_dbz", "echo_top_km",
                 "track_id", "lineage_id", "split_from")

    def __init__(self, d):
        self.seed_x, self.seed_y = d["seed_x"], d["seed_y"]
        self.area_km2, self.max_dbz = d["area_km2"], d["max_dbz"]
        self.echo_top_km = d["echo_top_km"]
        self.track_id = d["cell_uid"]
        self.lineage_id = d["family_uid"]
        # Lets the lifecycle metric tell a RECORDED split from an orphan birth.
        self.split_from = d["split_from"]


def parse_t(s):
    return datetime.strptime(s, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="/tmp/cases.json")
    ap.add_argument("--vols", type=int, default=10)
    ap.add_argument("--family-km", type=float, default=25.0)
    args = ap.parse_args()

    for case in json.load(open(args.cases)):
        site = case["sites"][0]
        tag = f"{case['wfo']}-{case['etn']}"
        t0, t1 = parse_t(case["issued"]), parse_t(case["expired"])
        vl = volume_keys(site, t0 - timedelta(minutes=30), t1 + timedelta(minutes=30))
        if len(vl) < 3:
            print(f"{tag}: {len(vl)} volumes -- skipped")
            continue
        mid = min(range(len(vl)), key=lambda i: abs(
            (vl[i][0] - (t0 + (t1 - t0) / 2)).total_seconds()))
        pick = vl[max(0, mid - args.vols // 2):][:args.vols]

        print(f"\n{'='*74}\n{tag}  {site}  streaming {len(pick)} volumes "
              f"(family distance {args.family_km:.0f} km)\n{'='*74}")
        H = StormHierarchy(site, P, HierarchyParams(
            family_distance_m=args.family_km * 1000.0))
        series, fam_series, env_recs = [], [], []
        prev_members: dict[int, set] = {}
        prev_env: dict[int, object] = {}
        overlap: list[float] = []
        splits: list[tuple] = []
        seen_uid: set[int] = set()
        churn_num = churn_den = 0

        for ts, key in pick:
            try:
                vol, hit = get_grid(site, ts, key, grid_datatree, xradar,
                                    GriddedVolume)
            except Exception as e:
                print(f"  {ts:%H:%M}Z load failed ({type(e).__name__})")
                continue
            t_a = time.time()
            dets, feats = detect_volume(vol, P)
            t_det = time.time() - t_a
            t_a = time.time()
            cells, fams = H.update(vol, dets, feats)
            t_link = time.time() - t_a

            for f in fams:
                # A family envelope must wrap its members' union exactly.
                union = None
                for c in cells:
                    if c["family_uid"] == f["family_uid"]:
                        m = c["_det"].mask
                        union = m.copy() if union is None else (union | m)
                if union is not None:
                    env_recs.append(envelope_fidelity(vol, union, f["envelope_xy"]))
                cur = set(f["cell_uids"])
                old = prev_members.get(f["family_uid"])
                if old:
                    churn_num += len(cur ^ old)
                    churn_den += len(cur | old)
                prev_members[f["family_uid"]] = cur

                # Envelope overlap is the RIGHT continuity measure for a family.
                # Centroid linearity is not: a complex that legitimately absorbs
                # a neighbour moves its centroid a long way in one volume, and
                # is penalised for doing the physically correct thing.
                pe = prev_env.get(f["family_uid"])
                ce = f["envelope_xy"]
                if pe is not None and not pe.is_empty and not ce.is_empty:
                    inter = pe.intersection(ce).area
                    uni = pe.area + ce.area - inter
                    if uni > 0:
                        overlap.append(inter / uni)
                prev_env[f["family_uid"]] = ce

            # A split daughter must arrive already carrying its parent's record.
            # "n_obs == 1 on a cell whose split_from is set" would mean the
            # inheritance silently did not happen.
            for c in cells:
                if c["split_from"] is not None and c["cell_uid"] not in seen_uid:
                    n_obs = len(H.cells[c["cell_uid"]].history)
                    splits.append((c["cell_uid"], c["split_from"], n_obs,
                                   c["age_min"]))
                seen_uid.add(c["cell_uid"])

            series.append(TrackSeries(vol.valid_time, [Cell(c) for c in cells]))
            biggest = max(fams, key=lambda f: f["area_km2"], default=None)
            print(f"  {ts:%H:%M}Z {len(cells):3d} cells {len(fams):2d} families "
                  f"det {t_det:4.1f}s link {t_link:4.1f}s"
                  + (f"  | biggest fam #{biggest['family_uid']} "
                     f"{biggest['n_cells']:2d} cells {biggest['area_km2']:6.0f}km2 "
                     f"age {biggest['age_min']:4.0f}min "
                     f"dA {biggest['trend']['area_km2_per_min']:+7.1f}km2/min"
                     if biggest else ""))

        if len(series) < 2:
            continue
        print(format_report("cells (cell_uid)", evaluate_tracks(series)))
        print(format_report("families (family_uid)",
                            evaluate_tracks(series, id_attr="lineage_id")))
        if env_recs:
            iou = np.array([r["iou"] for r in env_recs])
            ex = np.array([r["excess_ratio"] for r in env_recs])
            ex = ex[np.isfinite(ex)]
            print(f"  family envelope: n={len(env_recs)} IoU {iou.mean():.3f} "
                  f"(min {iou.min():.3f}) excess {ex.mean():.2f}")
        if churn_den:
            print(f"  family membership churn: {churn_num/churn_den:.3f} "
                  f"(0 = stable membership, 1 = fully replaced each volume)")
        if splits:
            inherited = [s for s in splits if s[2] > 1]
            print(f"  split daughters: {len(splits)}, "
                  f"{len(inherited)} arrived with inherited history "
                  f"({len(inherited)/len(splits):.0%})")
            for uid, par, n_obs, age in splits[:4]:
                print(f"    cell #{uid} split from #{par}: "
                      f"{n_obs} inherited obs, age {age:.0f} min at birth")
            orphaned = [s for s in splits if s[2] <= 1]
            if orphaned:
                print(f"    !! {len(orphaned)} split daughters started EMPTY "
                      f"(parent had no history yet)")
        else:
            print("  split daughters: none in this window")
        if overlap:
            ov = np.array(overlap)
            print(f"  family envelope continuity (volume-to-volume IoU): "
                  f"mean {ov.mean():.3f}  median {np.median(ov):.3f}  "
                  f"frac>0.5 {np.mean(ov > 0.5):.3f}   HIGHER better")

        longest = sorted(H.families.values(), key=lambda n: -n.duration_min)[:3]
        for n in longest:
            tr = n.trend()
            print(f"  family #{n.uid}: {n.duration_min:4.0f} min, "
                  f"{len(n.history)} obs, dArea {tr['area_km2_per_min']:+7.1f} km2/min, "
                  f"dDBZ {tr['dbz_per_min']:+5.2f}/min, "
                  f"dCells {tr['cells_per_min']:+5.2f}/min")


if __name__ == "__main__":
    main()
