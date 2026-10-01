#!/usr/bin/env python3
"""Geometry + tracker smoke test on synthetic shapes -- no radar download.

Uses a hook echo (a C-shape) because that is the exact case where a convex hull
fails: the hull spans the open mouth of the C and declares the inflow notch to
be storm. If the numbers below do not show that, the fidelity metric is wrong.
"""
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, "/tmp/scitv")
from scit.envelope import envelope_fidelity, footprint_polygon_xy
from scit.models import GriddedVolume
from scit.track2 import OverlapTracker, TrackParams
from scit.trackmetrics import TrackSeries, evaluate_tracks, format_report
from scit.types import StormCell


def mkvol(ny=200, nx=200):
    return GriddedVolume(
        site="KTST", valid_time=datetime(2026, 5, 1, tzinfo=timezone.utc),
        reflectivity=np.zeros((10, ny, nx), np.float32),
        x=np.arange(nx) * 1000.0 - nx * 500.0,
        y=np.arange(ny) * 1000.0 - ny * 500.0,
        z=np.arange(10) * 500.0, lat0=37.65, lon0=-97.44)


vol = mkvol()
ny, nx = vol.y.size, vol.x.size
yy, xx = np.mgrid[0:ny, 0:nx]

shapes = {}
# C-shape / hook: annulus with the right third removed.
r = np.hypot(yy - 100, xx - 100)
ann = (r > 12) & (r < 26)
shapes["hook (C-shape)"] = ann & ~((xx > 100) & (abs(yy - 100) < 12))
# Bow echo: a crescent.
shapes["bow echo"] = (np.hypot(yy - 100, xx - 100) < 30) & \
                     (np.hypot(yy - 88, xx - 100) > 22)
# Two disjoint cores, as a split leaves behind.
shapes["split pair"] = (np.hypot(yy - 100, xx - 80) < 10) | \
                       (np.hypot(yy - 100, xx - 125) < 10)
# Control: a compact blob, where a convex hull should be nearly right.
shapes["compact blob"] = np.hypot(yy - 100, xx - 100) < 15

print(f"{'shape':<18}{'method':<11}{'IoU':>7}{'excess':>8}{'true km2':>10}"
      f"{'poly km2':>10}{'false km2':>11}")
print("-" * 76)
for name, m in shapes.items():
    for meth in ("convex", "concave", "footprint"):
        p = footprint_polygon_xy(vol, m, method=meth, simplify_frac=0.25)
        f = envelope_fidelity(vol, m, p)
        print(f"{name:<18}{meth:<11}{f['iou']:>7.3f}{f['excess_ratio']:>8.2f}"
              f"{f['true_km2']:>10.0f}{f['poly_km2']:>10.0f}"
              f"{f['false_area_km2']:>11.0f}")
    print("-" * 76)

# ---------------- tracker: a storm that splits, then the halves merge -------
t0 = datetime(2026, 5, 1, 22, 0, tzinfo=timezone.utc)


def cell(cx, cy, rad, dbz, area, t):
    mask = np.hypot(yy - cy, xx - cx) < rad
    poly = footprint_polygon_xy(vol, mask, method="footprint", simplify_frac=0.25)
    return StormCell(cell_id=1, site="KTST", valid_time=t,
                     seed_lon=0.0, seed_lat=0.0,
                     seed_x=float(vol.x[int(cx)]), seed_y=float(vol.y[int(cy)]),
                     max_dbz=dbz, area_km2=area, echo_top_km=10.0,
                     base_km=0.5, depth_km=9.5, n_levels=8,
                     envelope=poly, envelope_xy=poly)


# volumes 0-2 one storm drifting east; 3-5 it is split in two; 6-8 merged again
frames = []
for k in range(9):
    t = t0 + timedelta(minutes=5 * k)
    cx = 80 + 3 * k
    if k < 3:
        frames.append((t, [cell(cx, 100, 14, 55 + k, 600 + 20 * k, t)]))
    elif k < 6:
        frames.append((t, [cell(cx, 92, 10, 56, 320, t),
                           cell(cx, 110, 10, 54, 300, t)]))
    else:
        frames.append((t, [cell(cx, 101, 15, 58, 700, t)]))

import copy
from scit.params import DetectionParams
from scit.track import Tracker

P = DetectionParams()
old, new = Tracker(P), OverlapTracker(P, TrackParams())
so, sn = [], []
for t, cs in frames:
    a = [copy.copy(c) for c in cs]
    b = [copy.copy(c) for c in cs]
    old.update(a, t)
    new.update(b, t)
    so.append(TrackSeries(t, a))
    sn.append(TrackSeries(t, b))

print("\nSYNTHETIC LIFECYCLE: 1 storm -> splits in 2 -> merges back to 1")
print("(3 volumes in each state, 5 min apart, storm drifting east)")
print(format_report("Tracker (strict 1:1)", evaluate_tracks(so, near_km=30)))
print(format_report("OverlapTracker track_id", evaluate_tracks(sn, near_km=30)))
print(format_report("OverlapTracker lineage_id",
                    evaluate_tracks(sn, near_km=30, id_attr="lineage_id")))
print("\n  events:", [(e.kind, e.track_ids, e.child_ids) for e in new.events])
print("  track_ids per volume, old:", [sorted({c.track_id for c in s.cells}) for s in so])
print("  track_ids per volume, new:", [sorted({c.track_id for c in s.cells}) for s in sn])
print("  lineages  per volume, new:", [sorted({c.lineage_id for c in s.cells}) for s in sn])
for tr in new.all_tracks():
    print(f"  track {tr.track_id} lineage {tr.lineage_id} parent {tr.parent_id} "
          f"dur {tr.duration_min:.0f}min trend {tr.trend()}")
