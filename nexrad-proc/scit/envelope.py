"""Storm envelopes that actually wrap the storm.

WHY THIS EXISTS
---------------
``identify._footprint_polygon`` returned ``MultiPoint(pts).convex_hull``. A convex
hull *bridges across* concavities, which is precisely wrong for the shapes that
matter most: a bow echo's trailing notch, a hook echo's inflow region, the gap
between the two halves of a splitting cell. The hull declares the empty air
inside the hook to be part of the storm.

Three methods are provided so the choice can be measured rather than asserted:

``footprint``  Exact raster boundary of the admitted cells, traced with marching
               squares at the half-level, so the polygon edge falls exactly on
               the grid-cell edge. Holes and disjoint parts are preserved. This
               is ground truth by construction (IoU 1.0); its only cost is
               vertex count.
``concave``    ``shapely.concave_hull`` at a given ratio -- a smoothed wrap that
               follows concavities without tracking every pixel stair-step.
``convex``     The old behaviour, kept so the regression is quantifiable.

All geometry is built in radar-relative x/y metres (where area is meaningful and
isotropic) and converted to lon/lat only on the way out.
"""

from __future__ import annotations

import numpy as np
import shapely
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import transform as shp_transform

__all__ = ["footprint_polygon_xy", "to_lonlat", "envelope_fidelity", "ENVELOPE_METHODS"]

ENVELOPE_METHODS = ("footprint", "concave", "convex")


def _grid_origin(volume):
    """(x0, y0, dx, dy) in metres. The grid is uniform by construction."""
    x, y = volume.x, volume.y
    dx = float(x[1] - x[0]) if x.size > 1 else volume.dx_km * 1000.0
    dy = float(y[1] - y[0]) if y.size > 1 else volume.dx_km * 1000.0
    return float(x[0]), float(y[0]), dx, dy


def _trace_footprint(mask: np.ndarray, volume) -> Polygon | MultiPolygon:
    """Exact cell-edge boundary of a boolean footprint mask, in x/y metres.

    Marching squares at level 0.5 on a mask padded by one cell puts each contour
    vertex exactly halfway between an inside and an outside cell centre -- i.e.
    on the physical grid-cell edge. Rings are then nested by containment so that
    an enclosed hole (a weak-echo region) is punched out rather than filled in.
    """
    from skimage.measure import find_contours

    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return Polygon()
    y0i, y1i = int(ys.min()), int(ys.max())
    x0i, x1i = int(xs.min()), int(xs.max())
    sub = mask[y0i : y1i + 1, x0i : x1i + 1]
    padded = np.pad(sub.astype(np.float32), 1)

    x0, y0, dx, dy = _grid_origin(volume)
    rings: list[Polygon] = []
    for c in find_contours(padded, 0.5):
        if c.shape[0] < 4:
            continue
        # padded row r -> original row (y0i + r - 1) -> metres
        gy = y0 + (y0i + c[:, 0] - 1.0) * dy
        gx = x0 + (x0i + c[:, 1] - 1.0) * dx
        p = Polygon(np.column_stack([gx, gy]))
        if not p.is_valid:
            p = p.buffer(0)
        if p.is_empty or p.area <= 0:
            continue
        rings.append(p)
    if not rings:
        # Single isolated cell: contour degenerates. Emit its box.
        return shapely.box(
            x0 + (x0i - 0.5) * dx, y0 + (y0i - 0.5) * dy,
            x0 + (x1i + 0.5) * dx, y0 + (y1i + 0.5) * dy,
        )

    # Largest-first so a ring is tested only against rings that could contain it.
    rings.sort(key=lambda p: p.area, reverse=True)
    depth = [sum(1 for o in rings[:i] if o.contains(p)) for i, p in enumerate(rings)]
    shells = [p for p, d in zip(rings, depth) if d % 2 == 0]
    holes = [p for p, d in zip(rings, depth) if d % 2 == 1]

    parts = []
    for s in shells:
        inner = [h.exterior.coords for h in holes if s.contains(h)]
        q = Polygon(s.exterior.coords, inner)
        if not q.is_valid:
            q = q.buffer(0)
        if not q.is_empty:
            parts.append(q)
    if not parts:
        return Polygon()
    return parts[0] if len(parts) == 1 else MultiPolygon(
        [g for p in parts for g in (p.geoms if hasattr(p, "geoms") else [p])]
    )


def footprint_polygon_xy(
    volume, mask: np.ndarray, method: str = "footprint", concave_ratio: float = 0.3,
    simplify_frac: float = 0.0,
) -> Polygon | MultiPolygon:
    """Envelope of ``mask`` in radar-relative x/y metres.

    ``simplify_frac`` is a Douglas-Peucker tolerance as a fraction of the grid
    spacing; 0 disables it. Anything below 0.5 cannot move an edge across a cell
    boundary, so it trims vertices without changing which cells are enclosed.
    """
    if method == "footprint":
        poly = _trace_footprint(mask, volume)
    else:
        ys, xs = np.nonzero(mask)
        if ys.size == 0:
            return Polygon()
        x0, y0, dx, dy = _grid_origin(volume)
        # Cell corners, not centres: a hull over centres clips half a cell off
        # every edge, which biases area low for small cells.
        cx, cy = x0 + xs * dx, y0 + ys * dy
        px = np.concatenate([cx - dx / 2, cx + dx / 2, cx - dx / 2, cx + dx / 2])
        py = np.concatenate([cy - dy / 2, cy - dy / 2, cy + dy / 2, cy + dy / 2])
        pts = shapely.multipoints(np.column_stack([px, py]))
        if method == "convex":
            poly = pts.convex_hull
        elif method == "concave":
            poly = shapely.concave_hull(pts, ratio=concave_ratio)
        else:
            raise ValueError(f"unknown envelope method {method!r}")
        if not isinstance(poly, (Polygon, MultiPolygon)):
            poly = poly.buffer(max(dx, dy) / 2)

    if simplify_frac > 0 and not poly.is_empty:
        _, _, dx, dy = _grid_origin(volume)
        poly = poly.simplify(simplify_frac * max(abs(dx), abs(dy)))
        if not poly.is_valid:
            poly = poly.buffer(0)
    return poly


def to_lonlat(volume, poly):
    """Reproject an x/y-metre polygon to lon/lat for storage and display."""
    if poly.is_empty:
        return poly

    def _t(x, y):
        lon, lat = volume.xy_to_lonlat(np.asarray(x), np.asarray(y))
        return np.atleast_1d(lon), np.atleast_1d(lat)

    return shp_transform(_t, poly)


def envelope_fidelity(volume, mask: np.ndarray, poly) -> dict:
    """How well ``poly`` wraps ``mask``.

    ``iou``          intersection-over-union against the true footprint cells.
    ``excess_ratio`` envelope area / true area. 1.0 is perfect; 2.0 means the
                     envelope claims twice the storm's actual area, which is the
                     convex-hull failure mode on hooks and bows.
    ``false_area_km2`` area enclosed that contains no storm -- the empty air a
                     downstream cylinder would sample as if it were the cell.
    """
    ys, xs = np.nonzero(mask)
    cell_km2 = volume.dx_km ** 2
    true_km2 = float(ys.size) * cell_km2
    if ys.size == 0 or poly.is_empty:
        return {"iou": 0.0, "excess_ratio": float("nan"),
                "true_km2": true_km2, "poly_km2": 0.0, "false_area_km2": 0.0}

    x0, y0, dx, dy = _grid_origin(volume)
    # Test every cell centre in the envelope's bounding box, so cells the
    # envelope wrongly includes are counted as well as cells it wrongly drops.
    minx, miny, maxx, maxy = poly.bounds
    i0 = max(0, int(np.floor((minx - x0) / dx)))
    i1 = min(volume.x.size - 1, int(np.ceil((maxx - x0) / dx)))
    j0 = max(0, int(np.floor((miny - y0) / dy)))
    j1 = min(volume.y.size - 1, int(np.ceil((maxy - y0) / dy)))
    if i1 < i0 or j1 < j0:
        return {"iou": 0.0, "excess_ratio": float("nan"), "true_km2": true_km2,
                "poly_km2": float(poly.area) / 1e6, "false_area_km2": 0.0}

    gx, gy = np.meshgrid(volume.x[i0 : i1 + 1], volume.y[j0 : j1 + 1])
    inside = shapely.contains_xy(poly, gx.ravel(), gy.ravel()).reshape(gx.shape)
    sub_true = mask[j0 : j1 + 1, i0 : i1 + 1]

    inter = float(np.count_nonzero(inside & sub_true))
    union = float(np.count_nonzero(inside | sub_true)) + float(
        np.count_nonzero(mask) - np.count_nonzero(sub_true)  # true cells outside bbox
    )
    poly_km2 = float(poly.area) / 1e6
    return {
        "iou": inter / union if union else 0.0,
        "excess_ratio": poly_km2 / true_km2 if true_km2 else float("nan"),
        "true_km2": true_km2,
        "poly_km2": poly_km2,
        "false_area_km2": max(0.0, poly_km2 - inter * cell_km2),
    }
