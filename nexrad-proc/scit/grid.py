"""Polar Zarr -> Cartesian GriddedVolume, without pyart.

WHY NOT PY-ART
--------------
The obvious route is `pyart.xradar.Xradar(dt)` + `pyart.map.grid_from_radars`.
Measured on this hardware, for one KEVX volume:

    lazy datatree  : wrap 110.99 s + grid 15.08 s = 126.07 s
    eager .load()  : wrap  32.79 s + grid 14.89 s =  47.68 s
    this module    :                                  0.59 s      (~80x)

Speed is the smaller reason. The bigger one is fidelity: pyart's Barnes2
interpolation with a range-dependent radius of influence spread sparse gates
across large volumes and **crushed peak reflectivity from 46.5 dBZ to 16.2 dBZ**,
while inflating "valid" cells from 24 % to 85 %. With `seed_dbz = 40`, that
means SCIT would never have seeded a single cell — the smoothing destroys
exactly the convective cores the detector keys on, and manufactures echo where
there is none.

Max-binning each gate into its cell preserves cores and leaves genuine gaps
genuinely empty. That is the right trade for cell identification (it is not the
right trade for a smooth display product — render those separately).

GEOMETRY
--------
Standard 4/3-effective-earth beam propagation:

    h = sqrt(r^2 + Ir^2 + 2 r Ir sin(e)) - Ir        beam height above radar
    s = Ir * asin(r cos(e) / (Ir + h))               great-circle ground range

with Ir = 4/3 * 6371 km. Ignoring refraction would misplace gates vertically by
hundreds of metres at range — which is precisely the axis `echo_top_min_km` and
`continuity_levels` gate on.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

IR = 4.0 / 3.0 * 6_371_000.0     # 4/3-earth effective radius, metres
REFL_FIELD = "DBZH"


def fill_vertical_gaps(grid: np.ndarray) -> np.ndarray:
    """Linearly interpolate NaN gaps WITHIN each column (never beyond its ends).

    WHY THIS IS REQUIRED, not cosmetic
    ----------------------------------
    A radar samples discrete elevation cuts — KOHX runs 0.48, 0.88, 1.27, 1.80,
    2.42, 3.08, 4.00 ... 19.51 degrees. At range the vertical spacing between
    consecutive beams exceeds the 0.5 km grid layer, so raw gate binning leaves
    holes. Measured on one KOHX column: occupied levels [3,4,6,8,10,13,16,21,26]
    — gaps of up to 5 layers.

    `identify()` builds seed components with 3D connectivity, so a column full of
    holes becomes a stack of disconnected single-layer slabs. The consequence was
    stark: of 2540 seed components in a volume with 65 dBZ convection, **2499
    (98 %) were rejected on `continuity_levels`**, including a 56 dBZ core with an
    8 km echo top sitting at `lv=1`. Real storms were being discarded for a
    sampling artifact.

    Interpolating within a column restores the continuity the radar actually
    observed, without pyart's horizontal smoothing that crushed 46.5 dBZ peaks
    down to 16.2. Max-bin horizontally, interpolate vertically — each axis gets
    the treatment it needs.

    Gaps are filled ONLY between the lowest and highest sampled layer of a
    column. Extrapolating past the ends would invent echo above the storm and
    inflate `echo_top_km`, corrupting the very gate this exists to serve.
    """
    nz = grid.shape[0]
    finite = np.isfinite(grid)
    if not finite.any():
        return grid

    zi = np.arange(nz, dtype=np.int32)[:, None, None]

    # index of the nearest sampled layer at or BELOW each layer
    below = np.where(finite, zi, -1)
    np.maximum.accumulate(below, axis=0, out=below)
    # ... and at or ABOVE
    above = np.where(finite, zi, nz)
    above = np.flip(np.minimum.accumulate(np.flip(above, axis=0), axis=0), axis=0)

    interior = (below >= 0) & (above < nz) & ~finite
    if not interior.any():
        return grid

    safe_b = np.clip(below, 0, nz - 1)
    safe_a = np.clip(above, 0, nz - 1)
    yy, xx = np.meshgrid(np.arange(grid.shape[1]), np.arange(grid.shape[2]), indexing="ij")
    vb = grid[safe_b, yy[None, :, :], xx[None, :, :]]
    va = grid[safe_a, yy[None, :, :], xx[None, :, :]]

    span = (safe_a - safe_b).astype(np.float32)
    span[span == 0] = 1.0
    w = (zi - safe_b).astype(np.float32) / span
    out = grid.copy()
    out[interior] = (vb + (va - vb) * w)[interior]
    return out


def grid_datatree(dt, site: str, h_m: float = 1000.0, v_m: float = 500.0,
                  range_m: float = 150_000.0, top_m: float = 15_000.0):
    """Bin every gate of every sweep into a Cartesian grid (max per cell).

    `dt` must already be in memory — call `.load()` first. Lazy access through a
    ZipStore turns this into thousands of tiny reads.
    """
    nx = ny = int(2 * range_m / h_m) + 1
    nz = int(top_m / v_m) + 1
    grid = np.full((nz, ny, nx), -np.inf, dtype=np.float32)
    # Second, CALIBRATED field: the mean of the gates falling in each cell.
    #
    # Max binning keeps peaks, which is what detection wants, but Zhang et al.
    # (2005) found maximum "would result in overestimation biases from the VPR
    # issue" -- and it is measurable here. ~40 % of cells receive more than one
    # gate, and among cells at or above 40 dBZ the max exceeds the mean by
    # 2.4 dB on a discrete-supercell volume and 4.1-4.2 dB on MCS volumes.
    # VIL integrates Z^(4/7), so 4.1 dB is a factor 2.6 in Z and roughly a 70 %
    # VIL overestimate. MESH inherits the same bias.
    #
    # The mean is accumulated in LINEAR Z (mm^6 m^-3) and converted back at the
    # end. dBZ is logarithmic, so averaging dBZ is not the mean of the physical
    # quantity VIL integrates -- it would understate exactly the strong returns
    # that matter. float64 because linear Z spans ~10 decades.
    ncell = nz * ny * nx
    zsum = np.zeros(ncell, dtype=np.float64)
    zcnt = np.zeros(ncell, dtype=np.int64)

    # Georeferencing lives on the ROOT as coords (FM301/CfRadial2), not on the
    # sweeps — `dt[sweep].ds["latitude"]` raises KeyError.
    root = dt.ds
    lat0 = float(np.asarray(root.coords["latitude"]).ravel()[0])
    lon0 = float(np.asarray(root.coords["longitude"]).ravel()[0])

    # Prefer the volume's own coverage start over a sweep's first ray time.
    vtime = None
    if "time_coverage_start" in root:
        raw = np.asarray(root["time_coverage_start"]).ravel()
        if raw.size:
            v = raw[0]
            try:
                vtime = (np.datetime64(v, "s").astype("datetime64[s]").astype(datetime)
                         if not isinstance(v, (bytes, str))
                         else datetime.fromisoformat(
                             (v.decode() if isinstance(v, bytes) else v).replace("Z", "+00:00")))
            except Exception:
                vtime = None

    gates = 0
    sweeps = [k for k in dt.children if k.startswith("sweep")]

    for sw in sweeps:
        ds = dt[sw].ds
        if REFL_FIELD not in ds:
            continue
        if vtime is None and "time" in ds.coords:
            t = np.asarray(ds.coords["time"]).ravel()
            if t.size:
                vtime = np.datetime64(t[0], "s").astype("datetime64[s]").astype(datetime)

        el = np.deg2rad(float(np.asarray(ds["sweep_fixed_angle"]).ravel()[0]))
        az = np.deg2rad(np.asarray(ds["azimuth"], dtype=np.float64))
        rg = np.asarray(ds["range"], dtype=np.float64)
        refl = np.asarray(ds[REFL_FIELD], dtype=np.float32)
        if refl.ndim != 2:
            continue

        h = np.sqrt(rg ** 2 + IR ** 2 + 2 * rg * IR * np.sin(el)) - IR
        s = IR * np.arcsin(rg * np.cos(el) / (IR + h))

        X = s[None, :] * np.sin(az)[:, None]
        Y = s[None, :] * np.cos(az)[:, None]
        Z = np.broadcast_to(h[None, :], X.shape)

        m = (np.isfinite(refl) & (np.abs(X) < range_m) & (np.abs(Y) < range_m)
             & (Z >= 0) & (Z < top_m))
        if not m.any():
            continue
        ix = ((X[m] + range_m) / h_m).astype(np.int64)
        iy = ((Y[m] + range_m) / h_m).astype(np.int64)
        iz = (Z[m] / v_m).astype(np.int64)
        vals = refl[m]
        np.maximum.at(grid, (iz, iy, ix), vals)
        # bincount on a flat index, not np.add.at: the latter is unbuffered and
        # an order of magnitude slower over millions of gates.
        flat = (iz * ny + iy) * nx + ix
        zsum += np.bincount(flat, weights=np.power(10.0, vals / 10.0),
                            minlength=ncell)
        zcnt += np.bincount(flat, minlength=ncell)
        gates += int(m.sum())

    grid[np.isneginf(grid)] = np.nan
    grid = fill_vertical_gaps(grid)

    with np.errstate(divide="ignore", invalid="ignore"):
        mean_lin = np.where(zcnt > 0, zsum / np.maximum(zcnt, 1), np.nan)
        grid_cal = (10.0 * np.log10(mean_lin)).astype(np.float32)
    grid_cal = fill_vertical_gaps(grid_cal.reshape(nz, ny, nx))

    x = (np.arange(nx) * h_m) - range_m
    y = (np.arange(ny) * h_m) - range_m
    z = np.arange(nz) * v_m

    if vtime is None:
        vtime = datetime.now(timezone.utc)
    if vtime.tzinfo is None:
        vtime = vtime.replace(tzinfo=timezone.utc)

    from .models import GriddedVolume
    vol = GriddedVolume(site=site, valid_time=vtime, reflectivity=grid,
                        x=x, y=y, z=z, lat0=lat0, lon0=lon0,
                        reflectivity_cal=grid_cal)
    return vol, gates
