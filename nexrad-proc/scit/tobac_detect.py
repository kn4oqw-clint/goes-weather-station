"""Per-volume storm detection with tobac's multi-threshold segmentation.

WHY THIS REPLACES ``identify``
------------------------------
``identify`` seeds at a single 40 dBZ contour and grows to a single 30 dBZ base
contour, so cell identity is dictated by two fixed intensity cutoffs. No
(seed, base, separation) tuple works for both a discrete supercell and an MCS,
because the regimes differ in intensity *structure*, not spacing. Measured: the
two detectors agree almost exactly on a discrete supercell (10.9 vs 11.2
cells/volume on ICT-7) and diverge ~8x on a squall line (37.3 vs 4.0 on
LOT-25), because the single-threshold detector cannot resolve cores embedded in
a contiguous >=30 dBZ region -- it returns the whole line as three objects.

Operational SCIT (Johnson et al. 1998) exists precisely to fix this, running at
seven thresholds and keeping, per storm, the highest threshold that yields a
valid-size component. ``tobac.feature_detection_multithreshold`` implements that
nested supersede rule directly.

WHAT IS KEPT FROM THE OLD PATH
------------------------------
The 3-D admission gates -- ``continuity_levels``, ``echo_top_min_km``,
``min_area_km2``. Those are why the rebuilt system cannot repeat the original's
failure of emitting tornado probabilities for cells under a kilometre tall, and
they are applied here per detected object, before anything becomes a cell.
Gate at identification, never at scoring.

Segmentation runs 2-D on composite reflectivity (Lakshmanan & Smith 2010 used
median-filtered composite, 30 dBZ floor, 20 km^2 minimum); the gates are then
evaluated against the full 3-D grid over each object's footprint.
"""

from __future__ import annotations

import numpy as np

from .envelope import footprint_polygon_xy, to_lonlat
from .params import DetectionParams

__all__ = ["detect_volume", "Detection", "SCIT_THRESHOLDS"]

# Johnson et al. (1998), Table 1 -- the operational SCIT threshold ladder.
SCIT_THRESHOLDS = (30.0, 35.0, 40.0, 45.0, 50.0, 55.0, 60.0)


class Detection:
    """One detected object in one volume, before it is given an identity."""

    __slots__ = ("local_id", "mask", "poly_xy", "seed_x", "seed_y",
                 "max_dbz", "area_km2", "echo_top_km", "base_km", "depth_km",
                 "n_levels", "hdim_1", "hdim_2", "threshold")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def _column_products(volume, params):
    """Per-column fields the gates need: echo top, seed-level count, composite."""
    refl = volume.reflectivity
    filled = np.where(np.isnan(refl), -np.inf, refl)
    comp = filled.max(axis=0)
    comp = np.where(np.isneginf(comp), np.nan, comp).astype(np.float32)

    top_ok = filled >= params.continuity_dbz
    zi = np.arange(refl.shape[0])[:, None, None]
    has = top_ok.any(axis=0)
    k_top = np.where(top_ok, zi, -1).max(axis=0)
    k_base = np.where(top_ok, zi, refl.shape[0]).min(axis=0)
    z_km = np.asarray(volume.z) / 1000.0
    etop = np.where(has, z_km[np.clip(k_top, 0, len(z_km) - 1)], np.nan)
    ebase = np.where(has, z_km[np.clip(k_base, 0, len(z_km) - 1)], np.nan)
    nlev = (filled >= params.seed_dbz).sum(axis=0).astype(np.int16)
    return comp, etop.astype(np.float32), ebase.astype(np.float32), nlev


def detect_volume(volume, params: DetectionParams | None = None,
                  thresholds=SCIT_THRESHOLDS, n_min: int = 20,
                  sigma: float = 0.5):
    """Detect and gate the storm objects in one gridded volume.

    Returns ``(detections, feats)`` where ``feats`` is tobac's own DataFrame
    filtered to the admitted objects. The DataFrame is carried forward rather
    than rebuilt, because ``linking_trackpy`` and ``merge_split_MEST`` read
    columns tobac sets internally and reconstructing them by hand is a standing
    invitation to a silent version skew.
    """
    import tobac
    import xarray as xr

    params = params or DetectionParams()
    comp, etop, ebase, nlev = _column_products(volume, params)
    dxy = volume.dx_km * 1000.0
    field = np.nan_to_num(comp, nan=0.0)[None, :, :]

    da = xr.DataArray(
        field, dims=("time", "y", "x"),
        coords={"time": np.array([np.datetime64(
            volume.valid_time.replace(tzinfo=None), "ns")]),
            "y": np.asarray(volume.y), "x": np.asarray(volume.x)},
        name="reflectivity")

    feats = tobac.feature_detection_multithreshold(
        da, dxy=dxy, threshold=list(thresholds), target="maximum",
        position_threshold="weighted_diff", sigma_threshold=sigma,
        n_min_threshold=n_min)
    if feats is None or len(feats) == 0:
        return [], None

    # Segment at the base contour so every >=base_dbz pixel is claimed by the
    # feature it belongs to. The union of a family's footprints is then the
    # storm's full echo region -- stratiform included -- not just its cores.
    mask, feats = tobac.segmentation_2D(feats, da, dxy=dxy,
                                        threshold=params.base_dbz,
                                        target="maximum")
    lab = np.asarray(mask.values)[0]
    cell_area = volume.dx_km ** 2
    z_km = np.asarray(volume.z) / 1000.0
    dz_km = volume.dz_km or params.grid_v_km

    out: list[Detection] = []
    admitted: list[int] = []
    for _, row in feats.iterrows():
        fid = int(row["feature"])
        foot = lab == fid
        n = int(foot.sum())
        if n == 0:
            continue
        area = n * cell_area
        sub_top = etop[foot]
        if not np.isfinite(sub_top).any():
            continue
        top = float(np.nanmax(sub_top))
        base = float(np.nanmin(ebase[foot]))
        levels = int(nlev[foot].max())

        # --- the gates that keep sub-kilometre returns out of the system ---
        if not (levels >= params.continuity_levels
                and top >= params.echo_top_min_km
                and area >= params.min_area_km2):
            continue

        sub = np.where(foot, comp, np.nan)
        if not np.isfinite(sub).any():
            continue
        yi = int(np.clip(round(float(row["hdim_1"])), 0, volume.y.size - 1))
        xi = int(np.clip(round(float(row["hdim_2"])), 0, volume.x.size - 1))
        poly = footprint_polygon_xy(
            volume, foot, method=params.envelope_method,
            concave_ratio=params.envelope_concave_ratio,
            simplify_frac=params.envelope_simplify_frac)

        out.append(Detection(
            local_id=fid, mask=foot, poly_xy=poly,
            seed_x=float(volume.x[xi]), seed_y=float(volume.y[yi]),
            max_dbz=float(np.nanmax(sub)), area_km2=float(area),
            echo_top_km=top, base_km=base,
            depth_km=float(top - base + dz_km), n_levels=levels,
            hdim_1=float(row["hdim_1"]), hdim_2=float(row["hdim_2"]),
            threshold=float(row.get("threshold_value", np.nan))))
        admitted.append(fid)

    # Strongest first, matching the old detector's contract.
    out.sort(key=lambda d: (-d.max_dbz, -d.area_km2, d.seed_x, d.seed_y))
    return out, feats[feats["feature"].isin(admitted)].copy()
