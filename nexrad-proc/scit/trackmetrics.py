"""Objective evaluation of storm tracking -- Lakshmanan & Smith (2010).

WHY NOT POD/FAR/CSI
-------------------
Point-truth skill against storm reports measures whether the system makes good
*hazard calls*. This system explicitly does not make hazard calls; it reports
why a cell has potential, and that argument is only available if a storm keeps
one identity long enough to accumulate a history. So detection skill is a sanity
floor and **track continuity plus envelope fidelity is the objective**.

Lakshmanan & Smith, "An objective method of evaluating and devising
storm-tracking algorithms", Wea. Forecasting 25, 701-709 (2010), evaluates a
tracker without hand-labelled truth, using three properties a good tracker has:

1. **Duration** -- tracks persist rather than fragmenting.
2. **Linearity** -- storm centroids follow a smooth path, so the residual from
   a best-fit line is small. An identity swap between two storms shows up here
   as a large residual even though duration looks fine.
3. **Attribute consistency** -- area, peak reflectivity and echo top evolve
   smoothly along a track. A track that jumps between different storms has a
   ragged attribute series.

GAMING, AND WHY THE METRICS ARE REPORTED JOINTLY
------------------------------------------------
Each is individually cheatable and must never be optimised alone:

* Duration alone is maximised by associating everything to everything -- so
  linearity and attribute consistency must be reported beside it.
* Linearity alone is maximised by cutting every track into two-point segments,
  because a straight line through two points has exactly zero residual -- so
  the linearity statistic is restricted to tracks of >= ``min_fit_points``
  samples and weighted by duration.

LIFECYCLE CORRECTNESS (added here, not in the paper)
----------------------------------------------------
The paper's metrics reward smooth tracks but say nothing about whether a split
was *recorded as a split*. Two diagnostics close that gap:

``orphan_births``  a track is born beside a storm that was already there and
                   kept going -- an unrecorded split, and the moment the new
                   cell's growth history was thrown away.
``silent_deaths``  a track ends beside a storm that continues -- an unrecorded
                   merge, reported as a disappearance.

For a tracker that models splits and merges explicitly both should fall to
near zero; for a strict 1:1 tracker they count exactly what it cannot express.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

__all__ = ["TrackSeries", "evaluate_tracks", "format_report", "envelope_summary"]


@dataclass
class TrackSeries:
    """One volume's worth of tracked cells, in time order."""

    t: object                       # datetime
    cells: list                     # StormCell, each with .track_id


class _Combined:
    """One identity's footprint within a single volume."""

    __slots__ = ("seed_x", "seed_y", "area_km2", "max_dbz", "echo_top_km", "n")

    def __init__(self, x, y, area, dbz, top, n):
        self.seed_x, self.seed_y = x, y
        self.area_km2, self.max_dbz, self.echo_top_km, self.n = area, dbz, top, n


def _merge_cells(cs: list):
    if len(cs) == 1:
        c = cs[0]
        return _Combined(c.seed_x, c.seed_y, c.area_km2, c.max_dbz,
                         c.echo_top_km, 1)
    w = np.array([max(c.area_km2, 1e-9) for c in cs], float)
    return _Combined(
        float(np.average([c.seed_x for c in cs], weights=w)),
        float(np.average([c.seed_y for c in cs], weights=w)),
        float(sum(c.area_km2 for c in cs)),
        float(max(c.max_dbz for c in cs)),
        float(max(c.echo_top_km for c in cs)),
        len(cs),
    )


def _fit_residual_km(ts_min: np.ndarray, v: np.ndarray) -> float:
    """RMS residual of ``v`` about its best-fit line in time, in the units of v."""
    if ts_min.size < 3 or np.ptp(ts_min) <= 0:
        return float("nan")
    a, b = np.polyfit(ts_min, v, 1)
    return float(np.sqrt(np.mean((v - (a * ts_min + b)) ** 2)))


def evaluate_tracks(series: list[TrackSeries], *, min_fit_points: int = 3,
                    near_km: float = 20.0, id_attr: str = "track_id") -> dict:
    """Compute the L&S-2010 suite plus lifecycle diagnostics.

    ``id_attr`` selects the identity being scored -- ``track_id`` for the raw
    track, or ``lineage_id`` to score the family, which is the identity the
    evidence chain is actually accumulated against.
    """
    series = sorted(series, key=lambda s: s.t)
    if len(series) < 2:
        return {"error": "need >= 2 volumes"}

    # ---- gather per-identity time series ------------------------------
    # A lineage can hold several cells in the SAME volume (the daughters of a
    # split). Left alone they would enter the series as consecutive samples and
    # manufacture a position jump between the two daughters every volume --
    # inflating the centroid RMS and the speed statistics. Collapse them to one
    # family sample: area-weighted centroid, total area, peak intensity. That is
    # also the physically right view, since the family IS the storm complex.
    tracks: dict[int, list] = {}
    for k, s in enumerate(series):
        by_id: dict[int, list] = {}
        for c in s.cells:
            tid = getattr(c, id_attr, None)
            if tid is None or tid < 0:
                continue
            by_id.setdefault(tid, []).append(c)
        for tid, cs in by_id.items():
            tracks.setdefault(tid, []).append((k, s.t, _merge_cells(cs)))

    t0 = series[0].t
    n_vol = len(series)
    total_cells = sum(len(s.cells) for s in series)

    durations_min, durations_vol = [], []
    pos_rms, area_rms, dbz_rms = [], [], []
    speeds, impossible = [], 0

    for tid, pts in tracks.items():
        pts.sort(key=lambda p: p[0])
        tm = np.array([(p[1] - t0).total_seconds() / 60.0 for p in pts])
        durations_min.append(float(tm[-1] - tm[0]))
        durations_vol.append(len(pts))

        xs = np.array([p[2].seed_x for p in pts]) / 1000.0
        ys = np.array([p[2].seed_y for p in pts]) / 1000.0
        for a, b in zip(pts[:-1], pts[1:]):
            dt = (b[1] - a[1]).total_seconds()
            if dt <= 0:
                continue
            v = math.hypot(b[2].seed_x - a[2].seed_x, b[2].seed_y - a[2].seed_y) / dt
            speeds.append(v)
            if v > 45.0:                     # faster than any storm motion
                impossible += 1

        if len(pts) >= min_fit_points:
            rx = _fit_residual_km(tm, xs)
            ry = _fit_residual_km(tm, ys)
            if np.isfinite(rx) and np.isfinite(ry):
                # Duration-weighted so 3-point stubs cannot dominate the mean.
                pos_rms.append((math.hypot(rx, ry), tm[-1] - tm[0]))
            ar = np.array([p[2].area_km2 for p in pts], float)
            if ar.mean() > 0:
                r = _fit_residual_km(tm, ar)
                if np.isfinite(r):
                    area_rms.append((r / ar.mean(), tm[-1] - tm[0]))
            dz = np.array([p[2].max_dbz for p in pts], float)
            r = _fit_residual_km(tm, dz)
            if np.isfinite(r):
                dbz_rms.append((r, tm[-1] - tm[0]))

    def _wmean(pairs):
        if not pairs:
            return float("nan")
        v = np.array([p[0] for p in pairs], float)
        w = np.array([max(p[1], 1e-6) for p in pairs], float)
        return float(np.average(v, weights=w))

    span_min = (series[-1].t - series[0].t).total_seconds() / 60.0
    span_hr = max(span_min / 60.0, 1e-6)
    live_hr = max(sum(durations_min) / 60.0, 1e-6)

    lifecycle = _lifecycle(series, near_km, id_attr)

    return {
        "volumes": n_vol,
        "span_min": span_min,
        "cells_total": total_cells,
        "cells_per_volume": total_cells / n_vol,
        "tracks": len(tracks),
        # -- 1. duration
        "duration_mean_min": float(np.mean(durations_min)) if durations_min else 0.0,
        "duration_median_min": float(np.median(durations_min)) if durations_min else 0.0,
        "duration_max_min": float(np.max(durations_min)) if durations_min else 0.0,
        "vols_mean": float(np.mean(durations_vol)) if durations_vol else 0.0,
        "frac_ge3_vols": (float(np.mean([d >= 3 for d in durations_vol]))
                          if durations_vol else 0.0),
        "frac_singleton": (float(np.mean([d == 1 for d in durations_vol]))
                           if durations_vol else 0.0),
        # -- 2. linearity (lower is better)
        "pos_rms_km": _wmean(pos_rms),
        "n_fit": len(pos_rms),
        # -- 3. attribute consistency (lower is better)
        "area_rms_frac": _wmean(area_rms),
        "dbz_rms": _wmean(dbz_rms),
        # -- fragmentation
        "new_ids_per_hour": len(tracks) / span_hr,
        "ids_per_storm_hour": len(tracks) / live_hr,
        # -- physical sanity
        "speed_mean_ms": float(np.mean(speeds)) if speeds else float("nan"),
        "speed_p95_ms": float(np.percentile(speeds, 95)) if speeds else float("nan"),
        "impossible_jumps": impossible,
        **lifecycle,
    }


def _radius_km(c) -> float:
    """Equivalent-circle radius of an object's footprint, in km."""
    a = getattr(c, "area_km2", 0.0) or 0.0
    return math.sqrt(max(a, 0.0) / math.pi)


def _lifecycle(series: list[TrackSeries], near_km: float, id_attr: str) -> dict:
    """Count births/deaths that are really unrecorded splits/merges.

    A birth beside a surviving storm is a split. The question is whether the
    tracker *recorded* it as one. If a cell carries ``split_from``, the split is
    on the record and its parent's history was inherited -- that is a success,
    not an orphan. Counting those as orphans made the metric read *worse* the
    moment split inheritance started working, which is exactly backwards.
    """
    seen: set[int] = set()
    orphan_births = births = recorded_splits = 0
    alive_prev: dict[int, object] = {}

    for k, s in enumerate(series):
        alive_now = {}
        for c in s.cells:
            tid = getattr(c, id_attr, None)
            if tid is None or tid < 0:
                continue
            alive_now[tid] = c
        if k > 0:
            survivors = [alive_prev[t] for t in alive_prev if t in alive_now]
            for tid, c in alive_now.items():
                if tid in seen:
                    continue
                births += 1
                # "Adjacent" must mean the same thing the tracker's split test
                # means, or the two disagree for no physical reason. A fixed
                # 20 km radius calls a storm 15 km away a split candidate even
                # when the footprints never touched; scaling the radius to the
                # objects' own size keeps the proxy close to the containment
                # test that actually drives inheritance.
                adjacent = False
                for p in survivors:
                    d = math.hypot(c.seed_x - p.seed_x,
                                   c.seed_y - p.seed_y) / 1000.0
                    reach = min(near_km,
                                _radius_km(c) + _radius_km(p))
                    if d <= reach:
                        adjacent = True
                        break
                if not adjacent:
                    continue                      # a genuine new storm
                if getattr(c, "split_from", None) not in (None, -1):
                    recorded_splits += 1
                else:
                    orphan_births += 1
        seen |= set(alive_now)
        alive_prev = alive_now

    # deaths: last seen at k, absent at k+1
    silent_deaths = deaths = 0
    for k in range(len(series) - 1):
        cur = {getattr(c, id_attr, -1): c for c in series[k].cells}
        nxt = {getattr(c, id_attr, -1): c for c in series[k + 1].cells}
        surviving_next = [nxt[t] for t in nxt if t in cur]
        for tid, c in cur.items():
            if tid < 0 or tid in nxt:
                continue
            # Only count a real disappearance: absent for the rest of the run.
            if any(tid in {getattr(x, id_attr, -1) for x in series[m].cells}
                   for m in range(k + 1, len(series))):
                continue
            deaths += 1
            for p in surviving_next:
                if math.hypot(c.seed_x - p.seed_x,
                              c.seed_y - p.seed_y) / 1000.0 <= near_km:
                    silent_deaths += 1
                    break

    split_like = recorded_splits + orphan_births
    return {
        "births": births, "orphan_births": orphan_births,
        "recorded_splits": recorded_splits,
        # Of the births that LOOK like splits, how many were recorded as one
        # (and so inherited their parent's history). This is the number to read.
        "split_coverage": recorded_splits / split_like if split_like else 1.0,
        "orphan_birth_rate": orphan_births / births if births else 0.0,
        "deaths": deaths, "silent_deaths": silent_deaths,
        "silent_death_rate": silent_deaths / deaths if deaths else 0.0,
    }


def envelope_summary(records: list[dict]) -> dict:
    """Aggregate per-cell :func:`envelope.envelope_fidelity` dicts."""
    if not records:
        return {}
    iou = np.array([r["iou"] for r in records], float)
    ex = np.array([r["excess_ratio"] for r in records], float)
    ex = ex[np.isfinite(ex)]
    fa = np.array([r["false_area_km2"] for r in records], float)
    tr = np.array([r["true_km2"] for r in records], float)
    return {
        "n": len(records),
        "iou_mean": float(iou.mean()),
        "iou_p05": float(np.percentile(iou, 5)),
        "excess_mean": float(ex.mean()) if ex.size else float("nan"),
        "excess_p95": float(np.percentile(ex, 95)) if ex.size else float("nan"),
        "excess_max": float(ex.max()) if ex.size else float("nan"),
        "false_area_total_km2": float(fa.sum()),
        "false_area_frac": float(fa.sum() / tr.sum()) if tr.sum() else float("nan"),
    }


_ROWS = [
    ("volumes", "volumes", "{:.0f}", ""),
    ("cells_per_volume", "cells/volume", "{:.1f}", ""),
    ("tracks", "tracks", "{:.0f}", ""),
    ("duration_mean_min", "duration mean", "{:.1f}", "min   HIGHER better"),
    ("duration_median_min", "duration median", "{:.1f}", "min"),
    ("vols_mean", "volumes/track", "{:.2f}", "      HIGHER better"),
    ("frac_ge3_vols", "frac >=3 vols", "{:.3f}", "      HIGHER better"),
    ("frac_singleton", "frac singleton", "{:.3f}", "      LOWER better"),
    ("pos_rms_km", "centroid RMS", "{:.2f}", "km    LOWER better  (L&S linearity)"),
    ("area_rms_frac", "area RMS/mean", "{:.3f}", "      LOWER better"),
    ("dbz_rms", "dBZ RMS", "{:.2f}", "dBZ   LOWER better"),
    ("ids_per_storm_hour", "ids/storm-hour", "{:.2f}", "      LOWER better  (fragmentation)"),
    ("speed_p95_ms", "speed p95", "{:.1f}", "m/s"),
    ("impossible_jumps", "impossible jumps", "{:.0f}", "      LOWER better"),
    ("split_coverage", "split coverage", "{:.3f}", "      HIGHER better  (splits recorded, history kept)"),
    ("orphan_birth_rate", "orphan births", "{:.3f}", "      LOWER better  (splits NOT recorded)"),
    ("silent_death_rate", "silent deaths", "{:.3f}", "      LOWER better  (unrecorded merges)"),
]


def format_report(name: str, m: dict) -> str:
    out = [f"--- {name} " + "-" * max(0, 56 - len(name))]
    for key, label, fmt, note in _ROWS:
        if key not in m:
            continue
        v = m[key]
        s = fmt.format(v) if isinstance(v, (int, float)) and np.isfinite(v) else "n/a"
        out.append(f"  {label:<20}{s:>10}  {note}")
    return "\n".join(out)
