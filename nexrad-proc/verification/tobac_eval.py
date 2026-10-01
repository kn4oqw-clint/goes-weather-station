#!/usr/bin/env python3
"""tobac multi-threshold + MEST merge/split, scored on the SAME objective.

tobac is the leading candidate because it supplies both halves of what is
needed: ``feature_detection_multithreshold`` implements the SCIT nested-threshold
supersede rule (Johnson et al. 1998) directly, and ``merge_split_MEST`` groups
linked cells into families -- which is exactly the lineage concept the
OverlapTracker introduces. The mapping is:

    tobac ``feature`` -> one detected cell in one volume
    tobac ``cell``    -> our ``track_id``   (a linked trajectory)
    tobac ``track``   -> our ``lineage_id`` (a family across splits/merges)

Scored with ``scit.trackmetrics`` so the numbers sit beside the in-house
detector's on the same axes. Envelope fidelity is measured from tobac's own
segmentation mask, so the comparison covers shape as well as identity.

Volumes come from ``volcache`` -- the column products, not the full 3-D grid --
so re-runs cost seconds rather than half an hour.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, os.environ.get("SCIT_SRC", "/tmp/scitv"))
sys.path.insert(0, "/tmp/pkgs")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import xradar                                                       # noqa: E402
from scit.envelope import envelope_fidelity, footprint_polygon_xy   # noqa: E402
from scit.grid import grid_datatree                                 # noqa: E402
from scit.models import GriddedVolume                               # noqa: E402
from scit.params import DetectionParams                             # noqa: E402
from scit.trackmetrics import (TrackSeries, envelope_summary,       # noqa: E402
                               evaluate_tracks, format_report)
from scit.types import StormCell                                    # noqa: E402
from volcache import get_volume, volume_keys                        # noqa: E402

warnings.filterwarnings("ignore")
P = DetectionParams()
# The seven operational SCIT thresholds (Johnson et al. 1998, Table 1).
THRESHOLDS = [30.0, 35.0, 40.0, 45.0, 50.0, 55.0, 60.0]

# CAUTION -- this setting silently changes what the metrics are measured over.
# tobac's default stubs=2 assigns cell = -1 to any feature it cannot link into a
# trajectory of at least two frames. Those features are then EXCLUDED from
# scoring, which flatters the singleton fraction by discarding exactly the
# objects that would have counted as singletons. stubs=1 gives every detection
# an id, which is what the streaming service does (a new core must be tracked
# the volume it appears in), and is the setting the two must be compared at.
STUBS = int(os.environ.get("TOBAC_STUBS", "1"))


def parse_t(s):
    return datetime.strptime(s, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)


class _Vol:
    """Enough of GriddedVolume for scit.envelope to work on cached fields."""

    def __init__(self, d):
        self.x, self.y = d["x"], d["y"]
        self.dx_km = float(d["dx_km"])
        self.lat0, self.lon0 = float(d["lat0"]), float(d["lon0"])
        self.colmax, self.etop_km, self.nlev_seed = (
            d["colmax"], d["etop_km"], d["nlev_seed"])
        self.valid_time = datetime.fromtimestamp(float(d["epoch"]), timezone.utc)

    def xy_to_lonlat(self, xm, ym):
        return GriddedVolume.xy_to_lonlat(self, xm, ym)

    def _to_lonlat(self):
        return GriddedVolume._to_lonlat(self)


def run_tobac(vols, dxy_m):
    import pandas as pd
    import tobac
    import xarray as xr

    stack = np.stack([np.nan_to_num(v.colmax, nan=0.0) for v in vols])
    times = [v.valid_time.replace(tzinfo=None) for v in vols]
    da = xr.DataArray(
        stack, dims=("time", "y", "x"),
        coords={"time": np.array(times, dtype="datetime64[ns]"),
                "y": vols[0].y, "x": vols[0].x},
        name="reflectivity")

    feats = tobac.feature_detection_multithreshold(
        da, dxy=dxy_m, threshold=THRESHOLDS, target="maximum",
        position_threshold="weighted_diff", sigma_threshold=0.5,
        n_min_threshold=20, statistic=None)
    if feats is None or len(feats) == 0:
        return None, None, None

    mask, feats = tobac.segmentation_2D(feats, da, dxy=dxy_m,
                                        threshold=P.base_dbz, target="maximum")

    dts = np.diff([v.valid_time.timestamp() for v in vols])
    dt = float(np.median(dts)) if dts.size else 300.0
    tracks = tobac.linking_trackpy(
        feats, da, dt=dt, dxy=dxy_m, v_max=30.0, method_linking="predict",
        adaptive_step=0.95, adaptive_stop=0.2, stubs=STUBS)

    lineage = {}
    try:
        from tobac.merge_split import merge_split_MEST
        ms = merge_split_MEST(tracks, dxy=dxy_m, distance=25000.0)
        cell_parent = np.asarray(ms["cell_parent_track_id"].values)
        cells = np.asarray(ms["cell"].values)
        lineage = {int(c): int(t) for c, t in zip(cells, cell_parent)}
    except Exception as e:                     # keep going without families
        print(f"  merge_split_MEST unavailable ({type(e).__name__}: {e})")
    return tracks, mask, lineage


def to_cells(tracks, mask, lineage, vols):
    """tobac features -> StormCell, applying the SAME admission gates."""
    series, env_recs = [], []
    mv = np.asarray(mask.values) if hasattr(mask, "values") else np.asarray(mask)
    for k, v in enumerate(vols):
        cell_area = v.dx_km ** 2
        sub = tracks[tracks["frame"] == k]
        lab = mv[k]
        cells = []
        for _, row in sub.iterrows():
            fid = int(row["feature"])
            foot = lab == fid
            n = int(foot.sum())
            if n == 0:
                continue
            area = n * cell_area
            etop = np.nanmax(np.where(foot, v.etop_km, np.nan))
            nlev = int(np.nanmax(np.where(foot, v.nlev_seed, 0)))
            dbz = float(np.nanmax(np.where(foot, v.colmax, np.nan)))
            if not (np.isfinite(etop) and nlev >= P.continuity_levels
                    and etop >= P.echo_top_min_km and area >= P.min_area_km2):
                continue
            poly = footprint_polygon_xy(v, foot, method=P.envelope_method,
                                        simplify_frac=P.envelope_simplify_frac)
            env_recs.append(envelope_fidelity(v, foot, poly))
            yi = int(np.clip(round(float(row["hdim_1"])), 0, v.y.size - 1))
            xi = int(np.clip(round(float(row["hdim_2"])), 0, v.x.size - 1))
            tid = int(row["cell"])
            c = StormCell(
                cell_id=fid, site="", valid_time=v.valid_time,
                seed_lon=0.0, seed_lat=0.0,
                seed_x=float(v.x[xi]), seed_y=float(v.y[yi]),
                max_dbz=dbz, area_km2=area, echo_top_km=float(etop),
                base_km=0.0, depth_km=float(etop), n_levels=nlev,
                envelope=poly, envelope_xy=poly,
                track_id=tid,
                # tobac marks unlinked features cell == -1; give each its own
                # id so they are not all pooled into one bogus track.
                lineage_id=lineage.get(tid, tid))
            if tid < 0:
                c.track_id = -1
                c.lineage_id = -1
            cells.append(c)
        series.append(TrackSeries(v.valid_time, cells))
    return series, env_recs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="/tmp/cases.json")
    ap.add_argument("--vols", type=int, default=10)
    args = ap.parse_args()

    all_env, all_series = [], []
    for case in json.load(open(args.cases)):
        site = case["sites"][0]
        tag = f"{case['wfo']}-{case['etn']}"
        t0, t1 = parse_t(case["issued"]), parse_t(case["expired"])
        vl = volume_keys(site, t0 - timedelta(minutes=30), t1 + timedelta(minutes=30))
        if len(vl) < 3:
            print(f"{tag}: only {len(vl)} volumes -- skipped")
            continue
        mid = min(range(len(vl)), key=lambda i: abs(
            (vl[i][0] - (t0 + (t1 - t0) / 2)).total_seconds()))
        lo = max(0, mid - args.vols // 2)
        pick = vl[lo:lo + args.vols]

        vols, nhit = [], 0
        for ts, key in pick:
            try:
                d = get_volume(site, ts, key, P, grid_datatree, xradar)
            except Exception as e:
                print(f"  {ts:%H:%M}Z load failed ({type(e).__name__})")
                continue
            nhit += bool(d.pop("cached", False))
            vols.append(_Vol(d))
        if len(vols) < 3:
            print(f"{tag}: only {len(vols)} usable volumes -- skipped")
            continue
        print(f"\n{'='*70}\n{tag} {site}: {len(vols)} volumes "
              f"({nhit} from cache)\n{'='*70}")

        tracks, mask, lineage = run_tobac(vols, vols[0].dx_km * 1000.0)
        if tracks is None:
            print(f"  {tag}: tobac found no features")
            continue
        series, envs = to_cells(tracks, mask, lineage, vols)
        all_env.extend(envs)
        all_series.append((tag, series))
        per = [len(s.cells) for s in series]
        print(f"  cells/volume: {per}  (mean {np.mean(per):.1f})")
        print(format_report(f"tobac {tag} by cell (=track_id)",
                            evaluate_tracks(series)))
        print(format_report(f"tobac {tag} by track (=lineage_id)",
                            evaluate_tracks(series, id_attr="lineage_id")))

    if all_env:
        s = envelope_summary(all_env)
        print(f"\ntobac envelope (footprint method): n={s['n']} "
              f"IoU {s['iou_mean']:.3f} excess {s['excess_mean']:.2f}")


if __name__ == "__main__":
    main()
