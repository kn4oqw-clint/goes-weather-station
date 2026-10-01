"""Minimal GriddedVolume — only what detection_v2 actually touches.

storm_modeler's version carries display products, per-sweep rasters and npz
serialisation for its GUI. SCIT itself uses just: site, valid_time,
reflectivity, x/y/z, dx_km, dz_km and xy_to_lonlat. Vendoring the small
surface keeps this container free of the desktop harness's dependencies.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from pyproj import Transformer

# pyproj transformer construction is not thread-safe in all builds
_PROJ_LOCK = threading.Lock()


@dataclass
class GriddedVolume:
    """A radar volume on a local azimuthal-equidistant Cartesian grid.

    x = metres east of the radar, y = metres north, z = metres AGL.
    reflectivity is (nz, ny, nx) dBZ with NaN where there is no echo.
    """

    site: str
    valid_time: datetime
    reflectivity: np.ndarray
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    lat0: float
    lon0: float
    # Calibrated reflectivity: the LINEAR-Z mean of the gates in each cell,
    # rather than their maximum. Use this for anything that integrates
    # calibrated reflectivity -- VIL, MESH -- because max binning overestimates
    # by 2.4 dB (discrete) to 4.2 dB (MCS) in cells at or above 40 dBZ, which is
    # roughly a 70 % VIL error. Keep `reflectivity` (max) for detection and for
    # display, where preserving the peak is the point. None if not computed.
    reflectivity_cal: np.ndarray | None = None

    @property
    def shape(self):
        return self.reflectivity.shape

    @property
    def dz_km(self) -> float:
        if self.z.size < 2:
            return 0.0
        return float(np.mean(np.diff(self.z))) / 1000.0

    @property
    def dx_km(self) -> float:
        if self.x.size < 2:
            return 1.0
        return float(np.mean(np.diff(self.x))) / 1000.0

    def _to_lonlat(self) -> Transformer:
        aeqd = (f"+proj=aeqd +lat_0={self.lat0} +lon_0={self.lon0} "
                "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs")
        return Transformer.from_crs(aeqd, "EPSG:4326", always_xy=True)

    def xy_to_lonlat(self, xm, ym):
        with _PROJ_LOCK:
            return self._to_lonlat().transform(xm, ym)

    def composite_reflectivity(self) -> np.ndarray:
        filled = np.where(np.isnan(self.reflectivity), -np.inf, self.reflectivity)
        comp = filled.max(axis=0)
        return np.where(np.isneginf(comp), np.nan, comp)
