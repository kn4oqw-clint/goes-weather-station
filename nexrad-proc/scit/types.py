"""Output types for the SCIT package."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from shapely.geometry import Polygon, mapping


@dataclass
class StormCell:
    """A single SCIT-identified storm cell within one volume.

    Attributes mirror the spec's cell contract (envelope polygon, seed,
    ``max_dbz``, ``area_km2``, ``echo_top_km``, ``depth_km``) plus the
    tracking identity (``track_id``) assigned across volumes.
    """

    cell_id: int  # sequential within the volume
    site: str
    valid_time: datetime
    seed_lon: float
    seed_lat: float
    seed_x: float  # metres east of radar
    seed_y: float  # metres north of radar
    max_dbz: float
    area_km2: float
    echo_top_km: float
    base_km: float
    depth_km: float
    n_levels: int
    envelope: Polygon  # storm envelope in lon/lat (see scit.envelope)
    # The same envelope in radar-relative x/y metres. The tracker associates on
    # footprint overlap, which needs an isotropic metric space; lon/lat is not
    # one. Not persisted -- lon/lat is the stored form.
    envelope_xy: object | None = None
    track_id: int = -1  # assigned by the tracker; -1 until tracked
    # The DURABLE identity. ``track_id`` changes when a storm splits, because
    # there are then two storms; ``lineage_id`` is the root ancestor and does
    # not, so the "has grown steadily" evidence chain survives the split.
    lineage_id: int = -1
    # Debug/verification only, populated by ``identify(keep_footprint=True)``:
    # the boolean footprint as (y0, y1, x0, x1) plus the sub-mask, so envelope
    # fidelity can be scored against the cells the envelope claims to wrap.
    footprint_bbox: tuple | None = None
    footprint_mask: object | None = None
    # GOES ABI cloud-top annotation, filled by ``cloudtop.associate_radar`` once
    # a satellite scene is matched to this cell (None until then).
    cloud_top_c: float | None = None  # cloud-top temperature, deg C
    # HRRR vault annotation, filled by ``vault.detect_vault`` once a
    # freezing-level grid is matched to this cell's volume (None/False until
    # then). Heights are km MSL. The overshooting-top flag is derived here —
    # the radar tower punching a set depth above the 0 °C level — not from GOES
    # (the ABI-based OT flag proved unreliable and was removed).
    freezing_level_km: float | None = None  # HRRR 0 °C isotherm height
    vault_top_km: float | None = None  # top of the >=vault_dbz echo tower
    vault_depth_km: float | None = None  # vault_top_km - freezing_level_km
    overshooting_top: bool = False  # vault_depth_km >= the configured minimum

    def summary(self) -> str:
        return (
            f"id {self.cell_id}  {self.max_dbz:.1f} dBZ  "
            f"depth {self.depth_km:.1f} km"
        )

    def to_dict(self) -> dict:
        return {
            "cell_id": self.cell_id,
            "track_id": self.track_id,
            "lineage_id": self.lineage_id,
            "site": self.site,
            "valid_time": self.valid_time.isoformat(),
            "seed_lon": self.seed_lon,
            "seed_lat": self.seed_lat,
            "seed_x": self.seed_x,
            "seed_y": self.seed_y,
            "max_dbz": self.max_dbz,
            "area_km2": self.area_km2,
            "echo_top_km": self.echo_top_km,
            "base_km": self.base_km,
            "depth_km": self.depth_km,
            "n_levels": self.n_levels,
            "cloud_top_c": self.cloud_top_c,
            "freezing_level_km": self.freezing_level_km,
            "vault_top_km": self.vault_top_km,
            "vault_depth_km": self.vault_depth_km,
            "overshooting_top": self.overshooting_top,
            "envelope": mapping(self.envelope),
        }
