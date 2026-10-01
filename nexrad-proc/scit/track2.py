"""Split/merge-aware storm tracking with lineage.

WHY THIS REPLACES ``track.Tracker``
-----------------------------------
The original tracker is greedy nearest-neighbour over seed centroids with a
``claimed`` set enforcing a strict 1:1 assignment. That constraint makes it
*structurally* unable to represent the two events that matter most:

* a **split** gives one daughter the ``track_id`` and the other a brand-new
  track with **zero history** -- the intensification record is destroyed at
  exactly the moment the storm does something worth alerting on;
* a **merge** lets one cell claim the track while the other track goes
  unmatched, ages out, and dies silently -- recorded as a disappearance.

No parameter fixes this: one track can never become two. It also tracked only
``(x, y)``, so "shrank" and "grew a new core" were not tracked at all.

WHAT THIS DOES INSTEAD
----------------------
Association is by **advected footprint overlap** (not centroid distance), which
is what survives a storm changing shape. The bipartite association graph is
decomposed into connected components and each component is classified:

    1 -> 1   continuation
    1 -> N   SPLIT   -- every daughter inherits the parent's history and lineage
    M -> 1   MERGE   -- the child records all contributing parents
    M -> N   complex -- resolved to best-overlap pairs, then labelled as above

**Lineage is the durable identity.** ``track_id`` changes when a storm splits,
because there are now two storms; ``lineage_id`` does not. The evidence chain
the comparator needs -- "this cell has steadily grown in intensity and area" --
is accumulated against the lineage, so a split no longer resets it to nothing.

Attribute history (area, peak dBZ, echo top) is carried on the track, so
shrinkage and new-core growth are first-class tracked quantities rather than
something to be reconstructed by joining per-volume rows on a broken id.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from .params import DetectionParams
from .types import StormCell

__all__ = ["OverlapTracker", "TrackState", "TrackEvent", "TrackParams"]


@dataclass(frozen=True)
class TrackParams:
    """Knobs for association. Defaults are for 1 km grids at 4--6 min cadence."""

    min_iou: float = 0.02          # any real overlap of advected footprints
    dist_gate_km: float = 15.0     # fallback gate for small/fast cells
    max_speed_ms: float = 45.0     # physical cap; scales the gate with dt
    miss_max: int = 2              # volumes a track may go unmatched
    motion_window: int = 4         # displacements averaged into the motion vector
    history_max: int = 120         # samples retained per track
    split_min_frac: float = 0.15   # daughter must hold this share of parent area
    merge_min_frac: float = 0.15   # parent must hold this share of child area


@dataclass
class Sample:
    t: datetime
    x: float
    y: float
    area_km2: float
    max_dbz: float
    echo_top_km: float


@dataclass
class TrackState:
    track_id: int
    lineage_id: int
    parent_id: int | None
    born: datetime
    last_t: datetime
    x: float
    y: float
    vx: float = 0.0                # m/s
    vy: float = 0.0
    misses: int = 0
    history: list[Sample] = field(default_factory=list)
    last_cell: StormCell | None = None
    _disp: list[tuple[float, float, float]] = field(default_factory=list)

    # -- evidence chain -------------------------------------------------
    def trend(self, window_min: float = 30.0) -> dict:
        """Slopes of area and peak reflectivity over the recent history.

        This is the "has it steadily grown" evidence, computed on the lineage's
        own record rather than inferred downstream. Returns NaN slopes until
        there are two samples to fit.
        """
        if len(self.history) < 2:
            return {"area_km2_per_min": float("nan"), "dbz_per_min": float("nan"),
                    "top_km_per_min": float("nan"), "n": len(self.history),
                    "span_min": 0.0}
        t_end = self.history[-1].t
        hs = [s for s in self.history
              if (t_end - s.t).total_seconds() <= window_min * 60.0]
        if len(hs) < 2:
            hs = self.history[-2:]
        tm = np.array([(s.t - hs[0].t).total_seconds() / 60.0 for s in hs])
        span = float(tm[-1] - tm[0])
        if span <= 0:
            return {"area_km2_per_min": float("nan"), "dbz_per_min": float("nan"),
                    "top_km_per_min": float("nan"), "n": len(hs), "span_min": 0.0}
        out = {"n": len(hs), "span_min": span}
        for key, vals in (("area_km2_per_min", [s.area_km2 for s in hs]),
                          ("dbz_per_min", [s.max_dbz for s in hs]),
                          ("top_km_per_min", [s.echo_top_km for s in hs])):
            out[key] = float(np.polyfit(tm, np.asarray(vals, float), 1)[0])
        return out

    @property
    def duration_min(self) -> float:
        return (self.last_t - self.born).total_seconds() / 60.0


@dataclass
class TrackEvent:
    kind: str                      # birth | split | merge | death | continue
    t: datetime
    track_ids: list[int]           # parents (split/merge/death) or [] for birth
    child_ids: list[int]           # children (split/merge/birth) or [] for death
    lineage_id: int | None = None


class _UF:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, a: int) -> int:
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _iou(a, b) -> float:
    if a is None or b is None or a.is_empty or b.is_empty:
        return 0.0
    try:
        inter = a.intersection(b).area
    except Exception:                      # topology exception on a bad ring
        a, b = a.buffer(0), b.buffer(0)
        inter = a.intersection(b).area
    if inter <= 0:
        return 0.0
    union = a.area + b.area - inter
    return float(inter / union) if union > 0 else 0.0


class OverlapTracker:
    """Stateful tracker. Feed it one volume's cells at a time, in time order."""

    def __init__(self, params: DetectionParams | None = None,
                 tp: TrackParams | None = None) -> None:
        self.params = params or DetectionParams()
        self.tp = tp or TrackParams()
        self._next_id = 1
        self.active: list[TrackState] = []
        self.retired: list[TrackState] = []
        self.events: list[TrackEvent] = []

    # -- internals ------------------------------------------------------
    def _spawn(self, c: StormCell, t: datetime, parent: TrackState | None = None
               ) -> TrackState:
        tid = self._next_id
        self._next_id += 1
        st = TrackState(
            track_id=tid,
            lineage_id=parent.lineage_id if parent else tid,
            parent_id=parent.track_id if parent else None,
            born=parent.born if parent else t,
            last_t=t, x=c.seed_x, y=c.seed_y,
        )
        if parent is not None:
            # A daughter inherits the family's record. Without this the split
            # would erase exactly the growth history we alert on.
            st.history = list(parent.history)[-self.tp.history_max:]
            st.vx, st.vy = parent.vx, parent.vy
            st._disp = list(parent._disp)
        self.active.append(st)
        return st

    def _advance(self, st: TrackState, c: StormCell, t: datetime) -> None:
        dt = (t - st.last_t).total_seconds()
        if dt > 0:
            vx, vy = (c.seed_x - st.x) / dt, (c.seed_y - st.y) / dt
            if math.hypot(vx, vy) <= self.tp.max_speed_ms:
                st._disp.append((vx, vy, dt))
                st._disp = st._disp[-self.tp.motion_window:]
                w = np.array([d[2] for d in st._disp])
                st.vx = float(np.average([d[0] for d in st._disp], weights=w))
                st.vy = float(np.average([d[1] for d in st._disp], weights=w))
        st.x, st.y, st.last_t, st.misses = c.seed_x, c.seed_y, t, 0
        st.last_cell = c
        st.history.append(Sample(t, c.seed_x, c.seed_y, c.area_km2,
                                 c.max_dbz, c.echo_top_km))
        st.history = st.history[-self.tp.history_max:]
        c.track_id = st.track_id
        setattr(c, "lineage_id", st.lineage_id)

    def _score(self, st: TrackState, c: StormCell, t: datetime
               ) -> tuple[float, float]:
        """(overlap IoU of the advected footprint, centroid distance in km).

        A physical displacement cap is applied FIRST and applies to overlap
        links as well as distance links. Without it, overlap association is
        actively dangerous on poorly segmented data: two 30,000 km^2 MCS blobs
        overlap substantially even with their centroids 100 km apart, so the
        tracker happily links them and reports a storm moving at 250 m/s.
        Overlap is only meaningful once objects are storm-scale.
        """
        dt = (t - st.last_t).total_seconds()
        if dt > 0:
            raw_ms = math.hypot(c.seed_x - st.x, c.seed_y - st.y) / dt
            if raw_ms > self.tp.max_speed_ms:
                return -1.0, float("inf")     # fails both gates
        px, py = st.x + st.vx * dt, st.y + st.vy * dt
        d_km = math.hypot(c.seed_x - px, c.seed_y - py) / 1000.0
        env_t = getattr(st.last_cell, "envelope_xy", None) if st.last_cell else None
        env_c = getattr(c, "envelope_xy", None)
        if env_t is None or env_c is None:
            return 0.0, d_km
        from shapely.affinity import translate
        return _iou(translate(env_t, xoff=st.vx * dt, yoff=st.vy * dt), env_c), d_km

    # -- public ---------------------------------------------------------
    def update(self, cells: list[StormCell], valid_time: datetime) -> list[StormCell]:
        t = valid_time
        if not self.active:
            for c in cells:
                self._advance(self._spawn(c, t), c, t)
                self.events.append(TrackEvent("birth", t, [], [c.track_id],
                                              getattr(c, "lineage_id", None)))
            return cells

        # Snapshot: _resolve spawns daughters onto self.active and drops merged
        # parents from it, so edge indices must address a stable list.
        prev = list(self.active)
        dt = max(1.0, min((t - st.last_t).total_seconds() for st in prev))
        gate_km = min(self.tp.dist_gate_km, self.tp.max_speed_ms * dt / 1000.0)
        gate_km = max(gate_km, self.params.grid_h_km * 3)

        nT, nC = len(prev), len(cells)
        edges: list[tuple[int, int, float]] = []
        for i, st in enumerate(prev):
            for j, c in enumerate(cells):
                iou, d = self._score(st, c, t)
                if iou >= self.tp.min_iou or d <= gate_km:
                    # Overlap dominates; distance only breaks ties among
                    # cells that do not overlap at all.
                    edges.append((i, j, iou + max(0.0, 1.0 - d / max(gate_km, 1e-6)) * 1e-3))
        if not edges:
            self._retire_unmatched(set(), t)
            for c in cells:
                self._advance(self._spawn(c, t), c, t)
                self.events.append(TrackEvent("birth", t, [], [c.track_id],
                                              getattr(c, "lineage_id", None)))
            return cells

        uf = _UF(nT + nC)
        for i, j, _ in edges:
            uf.union(i, nT + j)
        comps: dict[int, tuple[list[int], list[int]]] = {}
        for i, j, _ in edges:
            a, b = comps.setdefault(uf.find(i), ([], []))
            if i not in a:
                a.append(i)
            if j not in b:
                b.append(j)

        best = {}
        for i, j, s in edges:
            if (i, j) not in best or s > best[(i, j)]:
                best[(i, j)] = s

        matched_tracks: set[int] = set()
        for prev_idx, cur_idx in comps.values():
            self._resolve(prev, prev_idx, cur_idx, cells, best, t, matched_tracks)

        # Cells in no component at all are genuine new storms.
        seen_cells = {j for _, cur in comps.values() for j in cur}
        for j, c in enumerate(cells):
            if j not in seen_cells:
                self._advance(self._spawn(c, t), c, t)
                self.events.append(TrackEvent("birth", t, [], [c.track_id],
                                              getattr(c, "lineage_id", None)))

        self._retire_unmatched(matched_tracks, t)
        return cells

    def _resolve(self, prev, prev_idx, cur_idx, cells, best, t, matched):
        """Classify one connected component and apply it.

        ``prev`` is the snapshot of active tracks that the edge indices address.
        """
        # Assign every current cell to its best-scoring parent.
        assign: dict[int, int] = {}
        for j in cur_idx:
            cand = [(best[(i, j)], i) for i in prev_idx if (i, j) in best]
            if cand:
                assign[j] = max(cand)[1]
        groups: dict[int, list[int]] = {}
        for j, i in assign.items():
            groups.setdefault(i, []).append(j)

        # A parent that won no cell is a merge candidate -- and only such a
        # parent, because a parent that still owns a cell has plainly not
        # merged into anything. Testing "some other parent has an edge to this
        # child" instead would absorb a live neighbouring track and orphan the
        # cell it owned.
        losers = [k for k in prev_idx if k not in groups]
        merge_into: dict[int, list[int]] = {}
        for k in losers:
            cand = [(best[(k, j)], j) for j in cur_idx if (k, j) in best]
            if not cand:
                continue
            score, j = max(cand)
            # Require real footprint overlap, not mere proximity: a distance-only
            # edge scores <= 1e-3 by construction, an overlapping one >= min_iou.
            if score < self.tp.min_iou:
                continue
            child = cells[j]
            lc = prev[k].last_cell
            if lc is None or child.area_km2 <= 0:
                continue
            if lc.area_km2 < self.tp.merge_min_frac * child.area_km2:
                continue
            merge_into.setdefault(j, []).append(k)

        owner: dict[int, TrackState] = {}   # cell index -> the track that took it
        for i, js in groups.items():
            st = prev[i]
            matched.add(id(st))
            if len(js) == 1:
                self._advance(st, cells[js[0]], t)
                owner[js[0]] = st
                continue

            # One parent, several children: a SPLIT. Every daughter inherits the
            # parent's history and lineage -- the largest keeps the track_id
            # because it is the continuation a forecaster would call the storm,
            # the rest get new ids but the same family record.
            js.sort(key=lambda j: -cells[j].area_km2)
            snapshot = TrackState(**{**st.__dict__})
            self._advance(st, cells[js[0]], t)
            owner[js[0]] = st
            kids = [st.track_id]
            for j in js[1:]:
                child = self._spawn(cells[j], t, parent=snapshot)
                self._advance(child, cells[j], t)
                owner[j] = child
                kids.append(child.track_id)
            self.events.append(TrackEvent("split", t, [st.track_id], kids,
                                          st.lineage_id))

        # Apply merges last, so a cell that is simultaneously a split product
        # and a merge target is handled correctly rather than skipped.
        for j, ks in merge_into.items():
            st = owner.get(j)
            if st is None:
                continue
            absorbed = []
            for k in ks:
                o = prev[k]
                matched.add(id(o))
                absorbed.append(o.track_id)
                self.retired.append(o)
            drop = set(absorbed)
            self.active = [a for a in self.active if a.track_id not in drop]
            self.events.append(TrackEvent("merge", t, [st.track_id] + absorbed,
                                          [st.track_id], st.lineage_id))

    def _retire_unmatched(self, matched: set[int], t: datetime) -> None:
        survivors = []
        for st in self.active:
            if id(st) in matched or st.last_t == t:
                survivors.append(st)
                continue
            st.misses += 1
            if st.misses <= self.tp.miss_max:
                survivors.append(st)
            else:
                self.retired.append(st)
                self.events.append(TrackEvent("death", t, [st.track_id], [],
                                              st.lineage_id))
        self.active = survivors

    def all_tracks(self) -> list[TrackState]:
        return self.active + self.retired
