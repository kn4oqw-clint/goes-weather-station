"""Two-level storm identity: families of cells, each with its own history.

THE MODEL
---------
A squall line is one storm complex containing many cores. Both levels matter and
both are tracked, with independent histories:

``family``  the complex -- a whole squall line may legitimately be one or a few
            families. Its envelope is the union of its members' >=base_dbz
            footprints, so it covers the stratiform region too, not just cores.
            Its history carries total area, peak intensity, and **cell count**:
            a complex spawning new cores is a signal in its own right.

``cell``    a core within a family, tracked from the volume it first appears in.
            Carries its own area / intensity / echo-top history and trends, so a
            core that has been intensifying for 20 minutes is visible as such
            even though the family around it is steady.

THE STREAMING PROBLEM, AND HOW IT IS SOLVED
--------------------------------------------
tobac's ``linking_trackpy`` and ``merge_split_MEST`` are **batch** operations
over a complete time series. Production is a stream: one volume arrives at a
time and must be answered immediately.

This class runs a **rolling window**: the last ``window`` volumes' features are
re-linked on every new volume, which gives tobac's batch-quality linking. The
ids that batch produces are ephemeral -- they change on every re-run -- so they
are reconciled to **persistent uids** by majority vote over the frames the new
window shares with the previous one. A uid is only minted when no prior feature
supports one.

Consequences worth knowing:

* Re-linking can retroactively revise how *past* volumes would have been linked.
  Only the newest volume is emitted and persisted, so history already written is
  never rewritten -- ids stay stable going forward, and the majority vote is
  what keeps them stable.
* ``stubs=1`` in the linker is deliberate. tobac's default drops trajectories
  shorter than two frames, which would leave every newly-formed core unassigned
  until its second volume. A new core must be tracked the moment it is seen.
* ``memory`` lets a core vanish for a volume and be re-acquired rather than
  becoming a new cell -- the same role ``track_miss_max`` played before.
"""

from __future__ import annotations

import math
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import shapely

from .envelope import footprint_polygon_xy, to_lonlat
from .params import DetectionParams

__all__ = ["StormHierarchy", "Node", "Sample", "HierarchyParams"]


@dataclass(frozen=True)
class HierarchyParams:
    window: int = 12            # volumes re-linked each update (~1 h at 5 min)
    v_max_ms: float = 30.0      # trackpy displacement cap
    memory: int = 1             # volumes a cell may vanish for
    # merge_split_MEST search radius. 50 km chosen by sweep (famsweep.py) as the
    # LARGEST distance that still separates discrete storms:
    #
    #   dist   ICT-7 (discrete) fam/vol   LOT-25 (MCS) fam/vol   MCS env IoU
    #    10          10.0                      29.4                 0.246
    #    25           5.9                       5.2                 0.499
    #    50           2.6                       1.4                 0.742
    #   100           1.2                       1.0                 0.848
    #
    # At 25 km a squall line fragments into ~5 families, contradicting the
    # intent that a whole squall be one or a few. At 100 km the discrete case
    # collapses to 1.2 families/volume and separate storms stop being separate.
    # Envelope continuity keeps improving past 50 km, but that direction is
    # degenerate -- "group everything" is trivially stable, the same way
    # "associate everything" trivially maximised track duration. The binding
    # constraint is the discrete regime, not the MCS one.
    family_distance_m: float = 50000.0
    history_max: int = 240
    trend_window_min: float = 30.0
    # Fraction of a NEW object's footprint that must fall inside an existing
    # object's advected footprint for it to count as having split off that
    # object -- and therefore inherit its history.
    split_overlap_min: float = 0.5


@dataclass
class Sample:
    t: datetime
    x: float
    y: float
    area_km2: float
    max_dbz: float
    echo_top_km: float
    n_cells: int = 1


@dataclass
class Node:
    """A tracked identity -- a cell or a family -- and its record."""

    uid: int
    kind: str                       # "cell" | "family"
    born: datetime
    last_t: datetime
    parent_uid: int | None = None   # cell -> family; family -> None
    # The identity this one split away from. A daughter INHERITS that node's
    # history and birth time, so a core that breaks off a larger cell arrives
    # already carrying the record of what it broke off from. Without this a
    # split silently resets the evidence chain to nothing at exactly the moment
    # the storm does something worth alerting on.
    split_from: int | None = None
    history: list[Sample] = field(default_factory=list)

    @property
    def duration_min(self) -> float:
        return (self.last_t - self.born).total_seconds() / 60.0

    def observe(self, s: Sample, cap: int) -> None:
        self.history.append(s)
        if len(self.history) > cap:
            del self.history[:-cap]
        self.last_t = s.t

    def trend(self, window_min: float = 30.0) -> dict:
        """Growth slopes -- the evidence chain, computed on this node's record.

        This is what "has steadily grown in intensity and area" means
        operationally. Returned per minute so it is comparable across the
        varying volume cadence.
        """
        h = self.history
        if len(h) < 2:
            return {"n": len(h), "span_min": 0.0, "area_km2_per_min": float("nan"),
                    "dbz_per_min": float("nan"), "top_km_per_min": float("nan"),
                    "cells_per_min": float("nan")}
        t_end = h[-1].t
        hs = [s for s in h if (t_end - s.t).total_seconds() <= window_min * 60.0]
        if len(hs) < 2:
            hs = h[-2:]
        tm = np.array([(s.t - hs[0].t).total_seconds() / 60.0 for s in hs])
        span = float(tm[-1] - tm[0])
        if span <= 0:
            return {"n": len(hs), "span_min": 0.0, "area_km2_per_min": float("nan"),
                    "dbz_per_min": float("nan"), "top_km_per_min": float("nan"),
                    "cells_per_min": float("nan")}
        out = {"n": len(hs), "span_min": span}
        for key, vals in (("area_km2_per_min", [s.area_km2 for s in hs]),
                          ("dbz_per_min", [s.max_dbz for s in hs]),
                          ("top_km_per_min", [s.echo_top_km for s in hs]),
                          ("cells_per_min", [float(s.n_cells) for s in hs])):
            out[key] = float(np.polyfit(tm, np.asarray(vals, float), 1)[0])
        return out


class StormHierarchy:
    """Per-site. Feed it one volume's detections at a time, in time order."""

    def __init__(self, site: str, params: DetectionParams | None = None,
                 hp: HierarchyParams | None = None) -> None:
        self.site = site
        self.params = params or DetectionParams()
        self.hp = hp or HierarchyParams()
        self._win: deque = deque(maxlen=self.hp.window)   # (epoch, feats_df)
        self._cell_uid: dict[tuple[float, int], int] = {}   # (epoch, local) -> uid
        self._fam_uid: dict[tuple[float, int], int] = {}
        self.cells: dict[int, Node] = {}
        self.families: dict[int, Node] = {}
        self._next_cell = 1
        self._next_fam = 1
        # Previous volume's footprints, keyed by uid -- the evidence a
        # brand-new object split off an existing one.
        self._last_cell_foot: dict[int, tuple] = {}
        self._last_fam_foot: dict[int, tuple] = {}

    # -- reconciliation ---------------------------------------------------
    def _reconcile(self, groups: dict[int, list[tuple[float, int]]],
                   prior: dict[tuple[float, int], int],
                   mint) -> tuple[dict[int, int], dict[int, int | None]]:
        """Map ephemeral group ids to persistent uids by majority vote.

        ``groups`` maps an ephemeral id to the (epoch, local_feature) keys it
        contains. A group inherits the uid most of its keys already carry; the
        strongest claim wins a contested uid, so a genuine split hands the uid to
        the larger continuation rather than swapping identities at random.

        Returns ``(uid_map, parent_map)``. **When a group loses a contested uid
        it is a split daughter, and ``parent_map`` records the uid it split
        from** so the caller can hand it the parent's history. A group with no
        claim at all is a genuine new storm and has no parent.
        """
        scored = []
        for eph, keys in groups.items():
            votes = Counter(prior[k] for k in keys if k in prior)
            best, n = (votes.most_common(1)[0] if votes else (None, 0))
            scored.append((n, len(keys), eph, best))
        scored.sort(reverse=True)      # strongest claim first

        taken: set[int] = set()
        out: dict[int, int] = {}
        parent: dict[int, int | None] = {}
        for n, _, eph, best in scored:
            if best is not None and n > 0 and best not in taken:
                out[eph] = best
                taken.add(best)
                parent[eph] = None
            else:
                out[eph] = mint()
                # Lost a contested uid -> this branch split off `best`.
                parent[eph] = best if (best is not None and n > 0) else None
        return out, parent

    # -- main -------------------------------------------------------------
    def update(self, volume, detections, feats):
        """Assign identities for ``volume`` and return (cells, families).

        ``cells`` is a list of dicts, one per detection in this volume;
        ``families`` is a list of dicts, one per family present in this volume.
        """
        import pandas as pd
        import tobac

        epoch = volume.valid_time.timestamp()
        by_local = {d.local_id: d for d in detections}
        if feats is None or not len(feats) or not detections:
            self._win.append((epoch, None))
            return [], []
        self._win.append((epoch, feats.copy()))

        frames = [(e, f) for e, f in self._win if f is not None and len(f)]
        parts, keymap = [], {}
        gid = 1
        for fi, (e, f) in enumerate(frames):
            g = f.copy()
            g["frame"] = fi
            new_ids = list(range(gid, gid + len(g)))
            for nid, loc in zip(new_ids, g["feature"].tolist()):
                keymap[nid] = (e, int(loc))
            g["feature"] = new_ids
            gid += len(g)
            parts.append(g)
        allf = pd.concat(parts, ignore_index=True)

        dts = np.diff([e for e, _ in frames])
        dt = float(np.median(dts)) if dts.size else 300.0
        dxy = volume.dx_km * 1000.0

        try:
            tracks = tobac.linking_trackpy(
                allf, None, dt=dt, dxy=dxy, v_max=self.hp.v_max_ms,
                method_linking="predict", adaptive_step=0.95, adaptive_stop=0.2,
                memory=self.hp.memory, stubs=1)
        except Exception:
            # Linking failure must not lose the volume: fall back to one cell
            # per detection with no continuity rather than dropping everything.
            tracks = allf.copy()
            tracks["cell"] = tracks["feature"]

        cell_groups: dict[int, list] = {}
        for feat, cid in zip(tracks["feature"].tolist(), tracks["cell"].tolist()):
            cell_groups.setdefault(int(cid), []).append(keymap[int(feat)])

        fam_of_cell: dict[int, int] = {}
        try:
            from tobac.merge_split import merge_split_MEST
            ms = merge_split_MEST(tracks, dxy=dxy,
                                  distance=self.hp.family_distance_m)
            for c, t in zip(np.asarray(ms["cell"].values),
                            np.asarray(ms["cell_parent_track_id"].values)):
                fam_of_cell[int(c)] = int(t)
        except Exception:
            pass                       # each cell becomes its own family

        cmap, cpar = self._reconcile(cell_groups, self._cell_uid, self._mint_cell)
        fam_groups: dict[int, list] = {}
        for eph_cid, keys in cell_groups.items():
            fam_groups.setdefault(fam_of_cell.get(eph_cid, -eph_cid), []).extend(keys)
        fmap, fpar = self._reconcile(fam_groups, self._fam_uid, self._mint_fam)

        # Refresh the key->uid maps for every windowed frame, so the next
        # update votes against the current assignment.
        self._cell_uid = {k: cmap[e] for e, ks in cell_groups.items() for k in ks}
        self._fam_uid = {k: fmap[e] for e, ks in fam_groups.items() for k in ks}

        cell_parent = {cmap[e]: p for e, p in cpar.items() if p is not None}
        fam_parent = {fmap[e]: p for e, p in fpar.items() if p is not None}
        return self._emit(volume, by_local, epoch, cell_groups, cmap,
                          fam_of_cell, fmap, cell_parent, fam_parent)

    def _mint_cell(self):
        u = self._next_cell
        self._next_cell += 1
        return u

    def _mint_fam(self):
        u = self._next_fam
        self._next_fam += 1
        return u

    def _split_parent(self, store, last_foot, child_poly, t, kind: str):
        """Which existing object did this brand-new one break off?

        Re-link reconciliation only catches a *contested uid* -- i.e. tobac
        re-partitioning a trajectory between windows. A genuine physical split
        looks different: the daughter is a brand-new feature with no prior link
        at all, so it has no votes and would otherwise be minted with an empty
        history. That is the case that matters operationally, and it is found
        here geometrically: a new object sitting inside the footprint the parent
        occupied last volume (advected by the parent's own motion) IS the parent
        splitting.
        """
        if child_poly is None or child_poly.is_empty or child_poly.area <= 0:
            return None
        from shapely.affinity import translate
        best_uid, best_frac = None, 0.0
        for uid, (poly, pt) in last_foot.items():
            node = store.get(uid)
            if node is None or poly is None or poly.is_empty:
                continue
            dt = (t - pt).total_seconds()
            vx = vy = 0.0
            if len(node.history) >= 2:
                a, b = node.history[-2], node.history[-1]
                span = (b.t - a.t).total_seconds()
                if span > 0:
                    vx, vy = (b.x - a.x) / span, (b.y - a.y) / span
            try:
                p = translate(poly, xoff=vx * dt, yoff=vy * dt)
                frac = p.intersection(child_poly).area / child_poly.area
            except Exception:
                continue
            if frac > best_frac:
                best_frac, best_uid = frac, uid
        return best_uid if best_frac >= self.hp.split_overlap_min else None

    def _inherit(self, store: dict[int, "Node"], uid: int, kind: str,
                 t: datetime, parent_of: dict[int, int], fuid=None,
                 child_poly=None, last_foot=None) -> "Node":
        """Fetch or create a node, seeding a split daughter from its parent."""
        node = store.get(uid)
        if node is not None:
            return node
        pid = parent_of.get(uid)
        if pid is None and last_foot:
            pid = self._split_parent(store, last_foot, child_poly, t, kind)
        src = store.get(pid) if pid is not None else None
        node = Node(uid=uid, kind=kind,
                    # Inherit the parent's birth time too, so a daughter's age
                    # and trends describe the storm it came from rather than
                    # restarting the clock at the split.
                    born=src.born if src is not None else t,
                    last_t=t, parent_uid=fuid, split_from=pid)
        if src is not None:
            node.history = list(src.history)[-self.hp.history_max:]
        store[uid] = node
        return node

    def _emit(self, volume, by_local, epoch, cell_groups, cmap,
              fam_of_cell, fmap, cell_parent=None, fam_parent=None):
        t = volume.valid_time
        cap = self.hp.history_max
        tw = self.hp.trend_window_min
        cell_parent = cell_parent or {}
        fam_parent = fam_parent or {}

        # Which persistent uids are present in THIS volume.
        here: list[tuple[int, int, object]] = []       # (cell_uid, fam_uid, det)
        for eph_cid, keys in cell_groups.items():
            fam_uid = fmap[fam_of_cell.get(eph_cid, -eph_cid)]
            for e, loc in keys:
                if e != epoch:
                    continue
                d = by_local.get(loc)
                if d is not None:
                    here.append((cmap[eph_cid], fam_uid, d))

        out_cells = []
        for cuid, fuid, d in here:
            node = self._inherit(self.cells, cuid, "cell", t, cell_parent, fuid,
                                 child_poly=d.poly_xy,
                                 last_foot=self._last_cell_foot)
            node.parent_uid = fuid
            node.observe(Sample(t, d.seed_x, d.seed_y, d.area_km2, d.max_dbz,
                                d.echo_top_km), cap)
            lon, lat = volume.xy_to_lonlat(np.array([d.seed_x]),
                                           np.array([d.seed_y]))
            out_cells.append({
                "cell_uid": cuid, "family_uid": fuid,
                "split_from": node.split_from,
                "site": self.site, "valid_time": t,
                "seed_x": d.seed_x, "seed_y": d.seed_y,
                "seed_lon": float(np.atleast_1d(lon)[0]),
                "seed_lat": float(np.atleast_1d(lat)[0]),
                "max_dbz": d.max_dbz, "area_km2": d.area_km2,
                "echo_top_km": d.echo_top_km, "base_km": d.base_km,
                "depth_km": d.depth_km, "n_levels": d.n_levels,
                "envelope_xy": d.poly_xy,
                "envelope": to_lonlat(volume, d.poly_xy),
                "age_min": node.duration_min,
                "trend": node.trend(tw),
                "_det": d,
            })

        # Families: envelope is the union of member footprints, traced once as a
        # single mask so the result is the complex's true outline -- concavities
        # and interior gaps preserved -- rather than a hull over its cores.
        out_fams = []
        for fuid in sorted({f for _, f, _ in here}):
            members = [(c, d) for c, f, d in here if f == fuid]
            masks = [d.mask for _, d in members]
            union = masks[0].copy()
            for m in masks[1:]:
                union |= m
            poly = footprint_polygon_xy(
                volume, union, method=self.params.envelope_method,
                concave_ratio=self.params.envelope_concave_ratio,
                simplify_frac=self.params.envelope_simplify_frac)
            area = float(union.sum()) * volume.dx_km ** 2
            # Centroid of the family's ECHO REGION, not the weighted mean of its
            # cores. A core forming or dissipating shifts a core-weighted mean
            # discontinuously -- measured as a 47.7 m/s p95 family speed with
            # impossible jumps -- whereas the footprint centroid moves only as
            # far as the echo itself actually moved.
            ys, xs = np.nonzero(union)
            fx = float(np.asarray(volume.x)[xs].mean())
            fy = float(np.asarray(volume.y)[ys].mean())

            node = self._inherit(self.families, fuid, "family", t, fam_parent,
                                 child_poly=poly,
                                 last_foot=self._last_fam_foot)
            node.observe(Sample(t, fx, fy, area,
                                max(d.max_dbz for _, d in members),
                                max(d.echo_top_km for _, d in members),
                                len(members)), cap)
            lon, lat = volume.xy_to_lonlat(np.array([fx]), np.array([fy]))
            out_fams.append({
                "family_uid": fuid, "split_from": node.split_from,
                "site": self.site, "valid_time": t,
                "n_cells": len(members),
                "cell_uids": sorted(c for c, _ in members),
                "centroid_x": fx, "centroid_y": fy,
                "centroid_lon": float(np.atleast_1d(lon)[0]),
                "centroid_lat": float(np.atleast_1d(lat)[0]),
                "area_km2": area,
                "max_dbz": max(d.max_dbz for _, d in members),
                "echo_top_km": max(d.echo_top_km for _, d in members),
                "envelope_xy": poly,
                "envelope": to_lonlat(volume, poly),
                "age_min": node.duration_min,
                "trend": node.trend(tw),
            })

        # Footprints of THIS volume, so the next one can detect a split off them.
        self._last_cell_foot = {c["cell_uid"]: (c["envelope_xy"], t)
                                for c in out_cells}
        self._last_fam_foot = {f["family_uid"]: (f["envelope_xy"], t)
                               for f in out_fams}
        self._retire(t)
        return out_cells, out_fams

    def _retire(self, t: datetime, max_idle_min: float = 60.0) -> None:
        """Drop nodes not seen for a while so memory does not grow without end."""
        for store in (self.cells, self.families):
            dead = [u for u, n in store.items()
                    if (t - n.last_t).total_seconds() / 60.0 > max_idle_min]
            for u in dead:
                del store[u]
