#!/usr/bin/env python3
"""One-time (re-runnable) seeding of the offline map assets served to map.html.

Produces two files under /srv/goes/loop, both trimmed to the loop's crop box so
the browser is never handed continent-scale geometry:

  basemap.json  coastlines/national borders + state lines, from the Natural Earth
                GeoJSON goestools already ships locally -- no download, no tiles.
  ugc.json      UGC code -> polygon rings for NWS counties and public forecast
                zones. THIS is the only piece that needs the internet, exactly
                once: EMWIN watches/advisories identify their area by UGC code
                only, so without a local lookup they cannot be drawn. Warnings
                are unaffected -- they carry their own LAT...LON polygon over the
                dish (see alerts_geojson.py).

Re-run when the NWS republishes the shapefiles (a few times a year); the URLs are
discovered from their index pages rather than pinned to a dated filename.
"""
import io, json, os, re, sys, zipfile, urllib.request
import numpy as np

OUT = "/srv/goes/loop"
NE_DIR = "/usr/local/share/goestools/ne"
# Two different extents on purpose.
# BASEMAP must match make_loop.py's MAP_* (CONUS) box -- the map pans across the
# whole country, and coastlines trimmed to the old regional box left everything
# outside it as empty black.
BASEMAP_BOX = (-125.5, -66.5, 23.5, 50.0)
# UGC shapes only need to cover where alerts can actually come from. emwin_alerts
# filters to OFFICE=KMOB, so seeding all ~3200 CONUS counties + zones would be
# tens of MB of geometry that can never be drawn. Widen this if OFFICE ever does.
UGC_BOX = (-95.4, -78.6, 23.4, 37.9)
MARGIN = 1.5
NDP = 3                      # ~110 m; the loop is 0.0187 deg/px, so far sub-pixel
# Douglas-Peucker tolerance in degrees. County/zone outlines carry survey-grade
# vertex counts -- keeping them raw produced a 27 MB ugc.json, which is absurd to
# hand a browser. 0.004 deg is ~450 m, about a fifth of a pixel at the loop's
# native zoom, so this is invisible until you zoom well past the imagery's own
# 2 km resolution.
SIMPLIFY_EPS = 0.004
INDEX = {"county": "https://www.weather.gov/gis/Counties",
         "zone":   "https://www.weather.gov/gis/PublicZones"}
MONTHS = {"ja":1,"fe":2,"mr":3,"ap":4,"my":5,"jn":6,"jl":7,"au":8,"se":9,"oc":10,"no":11,"de":12}


def in_box(lons, lats, box):
    lo_min, lo_max, la_min, la_max = box
    return not (max(lons) < lo_min-MARGIN or min(lons) > lo_max+MARGIN or
                max(lats) < la_min-MARGIN or min(lats) > la_max+MARGIN)


def rdp(pts, eps):
    """Douglas-Peucker, iterative (recursion blows up on survey-grade rings) and
    vectorised per segment so a million-vertex pass stays seconds, not minutes."""
    n = len(pts)
    if n < 3:
        return pts
    a = np.asarray(pts, dtype=float)
    keep = np.zeros(n, dtype=bool); keep[0] = keep[-1] = True
    stack = [(0, n-1)]
    while stack:
        i, j = stack.pop()
        if j <= i+1:
            continue
        seg = a[i+1:j]
        p0, p1 = a[i], a[j]
        d = p1 - p0
        L = float(np.hypot(*d))
        if L == 0.0:
            dist = np.hypot(seg[:, 0]-p0[0], seg[:, 1]-p0[1])
        else:
            dist = np.abs(d[1]*seg[:, 0] - d[0]*seg[:, 1] + p1[0]*p0[1] - p1[1]*p0[0]) / L
        k = int(np.argmax(dist))
        if dist[k] > eps:
            m = i+1+k
            keep[m] = True
            stack.append((i, m)); stack.append((m, j))
    return a[keep].tolist()


def thin(ring):
    """Simplify, round to NDP, drop consecutive duplicates the rounding creates."""
    out = []
    for lon, lat in rdp(list(ring), SIMPLIFY_EPS):
        p = [round(lon, NDP), round(lat, NDP)]
        if not out or p != out[-1]:
            out.append(p)
    return out if len(out) >= 3 else None


# --------------------------------------------------------------------------- basemap
def rings_from_geojson(path, box):
    out = []
    with open(path) as fh:
        gj = json.load(fh)
    for feat in gj.get("features", []):
        geom = feat.get("geometry") or {}
        polys = geom.get("coordinates") or []
        if geom.get("type") == "Polygon":
            polys = [polys]
        elif geom.get("type") != "MultiPolygon":
            continue
        for poly in polys:
            for ring in poly:
                if len(ring) < 3:
                    continue
                lons = [c[0] for c in ring]; lats = [c[1] for c in ring]
                if not in_box(lons, lats, box):
                    continue
                t = thin(ring)
                if t:
                    out.append(t)
    return out


def build_basemap():
    data = {"countries": rings_from_geojson(os.path.join(NE_DIR, "ne_50m_admin_0_countries_lakes.json"), BASEMAP_BOX),
            "states":    rings_from_geojson(os.path.join(NE_DIR, "ne_50m_admin_1_states_provinces_lakes.json"), BASEMAP_BOX)}
    write(os.path.join(OUT, "basemap.json"), data)
    print("basemap.json: %d country rings, %d state rings" % (len(data["countries"]), len(data["states"])))


# --------------------------------------------------------------------------- UGC
def latest_zip(kind):
    """Newest shapefile URL off the NWS index page.

    Filenames are dated ddMMMyy (c_16ap26.zip = 16 Apr 2026). Page order is NOT
    newest-first and a plain string sort puts '18mr25' above '16ap26', so decode
    the date and take the true maximum."""
    html = urllib.request.urlopen(INDEX[kind], timeout=30).read().decode("utf8", "replace")
    rel = re.findall(r"""["'](/source/gis/[^"'\s>]+\.zip)""", html)
    if not rel:
        raise RuntimeError("no shapefile link found on " + INDEX[kind])
    def key(u):
        m = re.search(r"[cz]_(\d{2})([a-z]{2})(\d{2})\.zip$", u)
        if not m:
            return (0, 0, 0)
        d, mon, y = int(m.group(1)), MONTHS.get(m.group(2), 0), int(m.group(3))
        return (2000+y, mon, d)
    best = max(rel, key=key)
    if key(best) == (0, 0, 0):
        raise RuntimeError("no dated shapefile name matched on " + INDEX[kind])
    return "https://www.weather.gov" + best


def ugc_from_record(kind, rec):
    """UGC code for a shapefile record: STATE + C|Z + 3-digit county/zone id."""
    d = {k.lower(): v for k, v in rec.items()}
    state = (d.get("state") or "").strip()
    if not state:
        return None
    if kind == "county":
        fips = str(d.get("fips") or "").strip()
        return f"{state}C{fips[-3:]}" if len(fips) >= 3 else None
    z = str(d.get("zone") or "").strip()
    return f"{state}Z{z.zfill(3)}" if z else None


def build_ugc():
    import shapefile                                     # python3-pyshp
    shapes = {}
    for kind in ("county", "zone"):
        url = latest_zip(kind)
        print("fetching %s: %s" % (kind, url))
        blob = urllib.request.urlopen(url, timeout=180).read()
        zf = zipfile.ZipFile(io.BytesIO(blob))
        base = next(n[:-4] for n in zf.namelist() if n.lower().endswith(".shp"))
        r = shapefile.Reader(shp=io.BytesIO(zf.read(base+".shp")),
                             dbf=io.BytesIO(zf.read(base+".dbf")))
        kept = 0
        for sr in r.iterShapeRecords():
            ugc = ugc_from_record(kind, sr.record.as_dict())
            if not ugc:
                continue
            bb = sr.shape.bbox                            # (xmin,ymin,xmax,ymax)
            if not in_box([bb[0], bb[2]], [bb[1], bb[3]], UGC_BOX):
                continue
            pts = sr.shape.points
            parts = list(sr.shape.parts) + [len(pts)]
            rings = [t for t in (thin(pts[parts[i]:parts[i+1]]) for i in range(len(parts)-1)) if t]
            if rings:
                shapes.setdefault(ugc, []).extend(rings)
                kept += 1
        print("  %s: %d features in box" % (kind, kept))
    write(os.path.join(OUT, "ugc.json"), shapes)
    print("ugc.json: %d UGC codes" % len(shapes))


def write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    os.replace(tmp, path)
    print("  wrote %s (%.1f MB)" % (path, os.path.getsize(path)/1048576))


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    build_basemap()
    if "--basemap-only" in sys.argv:
        sys.exit(0)
    try:
        build_ugc()
    except Exception as e:
        print("UGC seeding FAILED (%s: %s)" % (type(e).__name__, e))
        print("map.html still draws storm-based warning polygons, which need no download.")
        sys.exit(1)
