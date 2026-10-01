"""The SCIT knob set — vendored from storm_modeler's settings resolver.

Every tunable is explicit and passed in; `detection_v2` reads no globals. The
`settings_hash` travels with every persisted cell so a detection can always be
traced back to the exact knob set that produced it.

THE GATES THAT MATTER: `echo_top_min_km`, `continuity_levels` and `min_area_km2`
are why the rebuilt system cannot repeat the old one's failure of emitting
tornado probabilities for cells under a kilometre tall. A candidate that does
not clear them never becomes a cell at all, so no downstream cylinder ever sees
it. Gate at identification, never at scoring.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class DetectionParams:
    seed_dbz: float = 40.0
    base_dbz: float = 30.0
    seed_min_separation_km: float = 6.0
    echo_top_min_km: float = 3.0
    continuity_dbz: float = 18.3
    continuity_levels: int = 3
    min_area_km2: float = 4.0
    grid_h_km: float = 1.0
    grid_v_km: float = 0.5
    watershed_split: bool = True
    watershed_min_sep_km: float = 14.0
    track_max_km: float = 12.0
    track_miss_max: int = 2
    # THE ENVELOPE. "footprint" traces the exact cell-edge boundary of the
    # admitted cells, so concavities -- a bow echo's notch, a hook's inflow --
    # are wrapped rather than bridged. "convex" is the original behaviour and
    # is kept only so the regression stays measurable; it declares the empty
    # air inside a hook to be part of the storm.
    envelope_method: str = "footprint"      # footprint | concave | convex
    envelope_concave_ratio: float = 0.3     # only for method="concave"
    envelope_simplify_frac: float = 0.25    # DP tolerance as a fraction of dx;
    #                                         < 0.5 cannot move an edge across a
    #                                         cell boundary, so it only trims
    #                                         vertices, never encloses new cells.

    @property
    def settings_hash(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @classmethod
    def from_env(cls, env: dict) -> "DetectionParams":
        vals = {}
        for f, t in cls.__dataclass_fields__.items():
            k = f"SCIT_{f.upper()}"
            if k in env:
                raw = env[k]
                if t.type in ("bool", bool):
                    vals[f] = str(raw).lower() in ("1", "true", "yes")
                elif t.type in ("int", int):
                    vals[f] = int(raw)
                elif t.type in ("str", str):
                    vals[f] = str(raw)
                else:
                    vals[f] = float(raw)
        return cls(**vals)
