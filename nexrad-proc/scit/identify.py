"""SCIT identification — gridded volume + params in, storm cells out.

Pure and testable: takes an explicit :class:`DetectionParams`, reads no globals.

Phase-1 guarantee: anomalous propagation never seeds. A seed candidate is
admitted only when it shows genuine vertical structure — presence across
``continuity_levels`` grid levels and an echo top above ``echo_top_min_km``,
measured against the ``continuity_dbz`` contour. Ground clutter (high
reflectivity confined to the lowest level) fails both gates.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import ndimage

from .envelope import footprint_polygon_xy, to_lonlat
from .models import GriddedVolume
from .params import DetectionParams
from .types import StormCell


def _suppress_close_seeds(cells: list[StormCell], min_sep_km: float) -> list[StormCell]:
    """Drop weaker cells whose centroid is within ``min_sep_km`` of a stronger one."""
    if min_sep_km <= 0:
        return cells
    kept: list[StormCell] = []
    # cells arrive strongest-first; greedily keep, suppressing close weaker ones.
    for c in cells:
        too_close = False
        for k in kept:
            d = math.hypot(c.seed_x - k.seed_x, c.seed_y - k.seed_y) / 1000.0
            if d < min_sep_km:
                too_close = True
                break
        if not too_close:
            kept.append(c)
    return kept


def identify(volume: GriddedVolume, params: DetectionParams | None = None,
             *, keep_footprint: bool = False) -> list[StormCell]:
    """Identify admitted storm cells in a single gridded volume.

    Deterministic: the same (volume, params) always yields the same cells in
    the same order (sorted by descending peak reflectivity, then footprint).

    ``keep_footprint`` attaches each cell's boolean footprint (as a bounding box
    plus sub-mask) for envelope-fidelity scoring. It is off in production
    because the masks are large and nothing downstream reads them.
    """
    params = params or DetectionParams()
    refl = volume.reflectivity  # (nz, ny, nx)
    nz, ny, nx = refl.shape
    dz_km = volume.dz_km or params.grid_v_km
    cell_area_km2 = volume.dx_km * volume.dx_km

    filled = np.nan_to_num(refl, nan=-9999.0)
    seed_mask = filled >= params.seed_dbz
    if not seed_mask.any():
        return []

    base_mask2d = (filled >= params.base_dbz).any(axis=0)  # (ny, nx) footprint extent
    top_mask = filled >= params.continuity_dbz  # echo-top / continuity contour

    structure = ndimage.generate_binary_structure(3, 1)  # 6-connected
    labels, n = ndimage.label(seed_mask, structure=structure)

    # Optional watershed split of merged seed blobs by per-column peak markers.
    if params.watershed_split and n >= 1:
        # Peak separation is a physical distance; convert to grid cells.
        min_sep_px = max(1, int(round(params.watershed_min_sep_km / volume.dx_km)))
        labels, n = _watershed_split(filled, seed_mask, labels, min_sep_px)

    # Footprint assignment. Without splitting, each seed grows to the whole
    # base-reflectivity component that contains it. With splitting on, that one
    # base component is partitioned among the seeds (a 2D watershed keyed by each
    # seed's column footprint), so a merged multi-core system resolves into
    # disjoint cells rather than N copies of the same whole-system envelope.
    if params.watershed_split and n >= 1:
        foot_labels = _partition_footprint(filled, labels, base_mask2d, n)
        base_labels = None
    else:
        foot_labels = None
        base_labels, _ = ndimage.label(base_mask2d)

    candidates: list[StormCell] = []
    for lab in range(1, n + 1):
        region = labels == lab
        zc, _, _ = np.nonzero(region)
        n_levels = int(np.unique(zc).size)

        seed_foot = region.any(axis=0)  # (ny, nx)
        if foot_labels is not None:
            foot = foot_labels == lab
            if not foot.any():  # seed claimed no base pixels — fall back to itself
                foot = seed_foot
        else:
            # Grow to the base-reflectivity footprint that contains this seed.
            overlap = base_labels[seed_foot]
            overlap = overlap[overlap > 0]
            if overlap.size:
                base_id = np.bincount(overlap).argmax()
                foot = base_labels == base_id
            else:
                foot = seed_foot

        ys_foot, xs_foot = np.nonzero(foot)
        area_km2 = float(ys_foot.size) * cell_area_km2

        col_top = top_mask[:, foot]
        z_present = np.nonzero(col_top.any(axis=1))[0]
        if z_present.size == 0:
            continue
        k_top, k_base = int(z_present.max()), int(z_present.min())
        echo_top_km = float(volume.z[k_top]) / 1000.0
        base_km = float(volume.z[k_base]) / 1000.0
        depth_km = (float(volume.z[k_top]) - float(volume.z[k_base])) / 1000.0 + dz_km

        max_dbz = float(np.nanmax(refl[region]))

        comp = np.where(region, np.nan_to_num(refl, nan=-np.inf), -np.inf).max(axis=0)
        w = comp[seed_foot]
        w = np.clip(np.where(np.isfinite(w), w, 0.0), 0.0, None)
        ys_seed, xs_seed = np.nonzero(seed_foot)
        if w.sum() <= 0:
            w = np.ones_like(w)
        seed_x = float(np.average(volume.x[xs_seed], weights=w))
        seed_y = float(np.average(volume.y[ys_seed], weights=w))
        seed_lon, seed_lat = volume.xy_to_lonlat(np.array([seed_x]), np.array([seed_y]))

        # --- Admission gates: what rejects AP -----------------------------
        admitted = (
            n_levels >= params.continuity_levels
            and echo_top_km >= params.echo_top_min_km
            and area_km2 >= params.min_area_km2
        )
        if not admitted:
            continue

        # Envelope in radar x/y metres first -- that is where area is isotropic
        # and where the tracker's overlap association operates -- then to
        # lon/lat for storage.
        poly_xy = footprint_polygon_xy(
            volume, foot, method=params.envelope_method,
            concave_ratio=params.envelope_concave_ratio,
            simplify_frac=params.envelope_simplify_frac,
        )
        cell = StormCell(
            cell_id=-1,
            site=volume.site,
            valid_time=volume.valid_time,
            seed_lon=float(np.atleast_1d(seed_lon)[0]),
            seed_lat=float(np.atleast_1d(seed_lat)[0]),
            seed_x=seed_x,
            seed_y=seed_y,
            max_dbz=max_dbz,
            area_km2=area_km2,
            echo_top_km=echo_top_km,
            base_km=base_km,
            depth_km=depth_km,
            n_levels=n_levels,
            envelope=to_lonlat(volume, poly_xy),
            envelope_xy=poly_xy,
        )
        if keep_footprint:
            y0, y1 = int(ys_foot.min()), int(ys_foot.max())
            x0, x1 = int(xs_foot.min()), int(xs_foot.max())
            cell.footprint_bbox = (y0, y1, x0, x1)
            cell.footprint_mask = foot[y0:y1 + 1, x0:x1 + 1].copy()
        candidates.append(cell)

    candidates.sort(key=lambda c: (-c.max_dbz, -c.area_km2, c.seed_x, c.seed_y))
    candidates = _suppress_close_seeds(candidates, params.seed_min_separation_km)
    for i, c in enumerate(candidates):
        c.cell_id = i + 1
    return candidates


def _partition_footprint(
    filled: np.ndarray, labels: np.ndarray, base_mask2d: np.ndarray, n: int
) -> np.ndarray:
    """Partition the base footprint among the seeds (2D watershed by seed column).

    Each base-threshold pixel is assigned to the seed it descends to along the
    column-max reflectivity surface, so a merged multi-core system resolves into
    disjoint cell footprints instead of N copies of the whole-system envelope.
    Returns a 2D label image (0 = unclaimed; seed label otherwise).
    """
    from skimage.segmentation import watershed

    colmax = filled.max(axis=0)  # (ny, nx) column-max reflectivity
    markers = np.zeros(base_mask2d.shape, dtype=np.int32)
    for lab in range(1, n + 1):
        markers[(labels == lab).any(axis=0)] = lab
    markers[~base_mask2d] = 0  # seeds sit at >= seed_dbz, so always within base
    return watershed(-colmax, markers=markers, mask=base_mask2d)


def _watershed_split(
    filled: np.ndarray,
    seed_mask: np.ndarray,
    labels: np.ndarray,
    min_distance: int = 3,
):
    """Split merged seed components at reflectivity saddles (skimage watershed).

    ``min_distance`` is the minimum peak separation in grid cells: larger values
    merge nearby cores, yielding fewer cells.
    """
    from scipy import ndimage as ndi
    from skimage.feature import peak_local_max
    from skimage.segmentation import watershed

    # Markers: local reflectivity maxima within the seed mask.
    coords = peak_local_max(
        np.where(seed_mask, filled, 0.0), min_distance=min_distance, labels=labels
    )
    if coords.shape[0] == 0:
        return labels, int(labels.max())
    markers = np.zeros_like(labels)
    for i, (z, y, x) in enumerate(coords, 1):
        markers[z, y, x] = i
    split = watershed(-filled, markers=markers, mask=seed_mask)
    return split, int(split.max())
