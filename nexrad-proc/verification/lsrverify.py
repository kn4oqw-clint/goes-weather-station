#!/usr/bin/env python3
"""LSR sanity floor for the tobac path -- POD / FAR / CSI against storm reports.

THIS IS A FLOOR, NOT THE OBJECTIVE. The system does not make hazard calls, so
detection skill against storm reports cannot be what it is tuned for; track
continuity and envelope fidelity are (see
docs/scit-track-continuity-objective.md). What this answers is the cruder and
still necessary question: **are we finding real storms at all?** A detector that
scored well on continuity while missing every reported tornado would be
producing beautifully stable tracks of nothing.

Scored at BOTH levels, because they fail differently:

  cell    over-segmentation shows up here as false alarms -- 37 cells/volume on
          a squall line means many cells match no report
  family  a complex is the thing a user is actually warned about, and there are
          far fewer of them, so FAR is the more meaningful figure

KNOWN BIAS -- do not read these as absolute skill. Storm reports are spatially
biased: rural areas under-report, so a genuine storm over empty country scores
as a false alarm. SHAVE's no-hail nulls exist to fix exactly this and are not
used here. Treat the numbers as RELATIVE, for ranking configurations.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.parse
import urllib.request
import warnings
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# SCIT_SRC LAST so it lands at sys.path[0] and wins. The script dir
# also contains a scit/ tree, and inserting it afterwards would shadow
# SCIT_SRC entirely -- which silently tested the wrong copy once.
sys.path.insert(0, os.environ.get("SCIT_SRC", "/app"))
warnings.filterwarnings("ignore")

import xradar                                                     # noqa: E402
from scit.grid import grid_datatree                               # noqa: E402
from scit.hierarchy import HierarchyParams, StormHierarchy        # noqa: E402
from scit.models import GriddedVolume                             # noqa: E402
from scit.params import DetectionParams                           # noqa: E402
from scit.tobac_detect import detect_volume                       # noqa: E402
from volcache import get_grid, volume_keys                        # noqa: E402

P = DetectionParams()
SEVERE = {"T", "H", "G", "D"}      # tornado, hail, wind gust, damage
MATCH_KM = float(os.environ.get("MATCH_KM", "15"))
MATCH_MIN = float(os.environ.get("MATCH_MIN", "15"))
MAX_RANGE_KM = 140.0


def hav(a, b):
    R = 6371.0
    dlat, dlon = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    x = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(a[0]))
         * math.cos(math.radians(b[0])) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


def fetch_lsr(t0, t1):
    q = urllib.parse.urlencode({"sts": t0.strftime("%Y-%m-%dT%H:%MZ"),
                                "ets": t1.strftime("%Y-%m-%dT%H:%MZ")})
    url = f"https://mesonet.agron.iastate.edu/geojson/lsr.geojson?{q}"
    with urllib.request.urlopen(url, timeout=90) as r:
        d = json.load(r)
    out = []
    for f in d.get("features", []):
        p = f["properties"]
        if p.get("type") not in SEVERE:
            continue
        try:
            ts = datetime.strptime(p["valid"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
        except Exception:
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        out.append({"t": ts, "lat": float(lat), "lon": float(lon),
                    "type": p["type"]})
    return out


def parse_t(s):
    return datetime.strptime(s, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)


def score(objs, reps, site_ll, latlon, envelope):
    """Object-vs-point contingency.

    A report matches an object if it falls INSIDE the object's envelope, or
    within ``MATCH_KM`` of the object's centroid.

    The containment test is not a refinement, it is the correct test at the
    family level. A storm complex can cover 35,000 km^2, so its centroid sits
    nowhere near any particular report -- scored on centroid distance alone a
    family that plainly contains a tornado report reads as a miss (measured:
    cell 4/4 reports detected while family scored 0/4 on the same volume).
    Centroid distance is retained only as a near-miss allowance for small
    objects and for reports just outside the echo.
    """
    reps = [r for r in reps if hav(site_ll, (r["lat"], r["lon"])) <= MAX_RANGE_KM]
    if not objs and not reps:
        return None
    from shapely.geometry import Point
    det, hit = set(), 0
    for o in objs:
        olat, olon = latlon(o)
        env = envelope(o)
        matched = False
        for j, r in enumerate(reps):
            inside = False
            if env is not None and not env.is_empty:
                try:
                    inside = env.contains(Point(r["lon"], r["lat"]))
                except Exception:
                    inside = False
            if inside or hav((olat, olon), (r["lat"], r["lon"])) <= MATCH_KM:
                det.add(j)
                matched = True
        hit += 1 if matched else 0
    return {"reports": len(reps), "detected": len(det), "objs": len(objs),
            "hits": hit, "fa": len(objs) - hit}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="/tmp/cases.json")
    ap.add_argument("--vols", type=int, default=8)
    args = ap.parse_args()

    agg = {k: {"reports": 0, "detected": 0, "objs": 0, "hits": 0, "fa": 0}
           for k in ("cell", "family")}

    for case in json.load(open(args.cases)):
        site = case["sites"][0]
        tag = f"{case['wfo']}-{case['etn']}"
        t0, t1 = parse_t(case["issued"]), parse_t(case["expired"])
        reports = fetch_lsr(t0 - timedelta(minutes=MATCH_MIN),
                            t1 + timedelta(minutes=MATCH_MIN))
        vl = volume_keys(site, t0 - timedelta(minutes=30),
                         t1 + timedelta(minutes=30))
        if len(vl) < 3:
            continue
        mid = min(range(len(vl)), key=lambda i: abs(
            (vl[i][0] - (t0 + (t1 - t0) / 2)).total_seconds()))
        pick = vl[max(0, mid - args.vols // 2):][:args.vols]

        print(f"\n{tag} {site}: {len(pick)} volumes, {len(reports)} severe LSRs")
        H = StormHierarchy(site, P, HierarchyParams())
        for ts, key in pick:
            try:
                vol, _ = get_grid(site, ts, key, grid_datatree, xradar,
                                  GriddedVolume)
                dets, feats = detect_volume(vol, P)
                cells, fams = H.update(vol, dets, feats)
            except Exception as e:
                print(f"  {ts:%H:%M}Z skip ({type(e).__name__}: {e})")
                continue
            near = [r for r in reports
                    if abs((r["t"] - ts).total_seconds()) <= MATCH_MIN * 60]
            site_ll = (vol.lat0, vol.lon0)
            sc = score(cells, near, site_ll,
                       lambda c: (c["seed_lat"], c["seed_lon"]),
                       lambda c: c["envelope"])
            sf = score(fams, near, site_ll,
                       lambda f: (f["centroid_lat"], f["centroid_lon"]),
                       lambda f: f["envelope"])
            line = f"  {ts:%H:%M}Z reports={len(near):2d}"
            for name, s in (("cell", sc), ("family", sf)):
                if s:
                    for k in agg[name]:
                        agg[name][k] += s[k]
                    line += f" | {name} {s['detected']}/{s['reports']}r {s['objs']}o"
            print(line)

    print(f"\n{'level':<9}{'reports':>9}{'detected':>10}{'objects':>9}{'FA':>7}"
          f"{'POD':>7}{'FAR':>7}{'CSI':>7}")
    print("-" * 65)
    for name in ("cell", "family"):
        a = agg[name]
        pod = a["detected"] / a["reports"] if a["reports"] else 0.0
        far = a["fa"] / a["objs"] if a["objs"] else 0.0
        csi = a["detected"] / (a["reports"] + a["fa"]) if (a["reports"] + a["fa"]) else 0.0
        print(f"{name:<9}{a['reports']:>9}{a['detected']:>10}{a['objs']:>9}"
              f"{a['fa']:>7}{pod:>7.3f}{far:>7.3f}{csi:>7.3f}")
    print("-" * 65)
    print("SANITY FLOOR ONLY. LSR truth under-reports rural storms, so FAR is")
    print("pessimistic. These rank configurations; they are not absolute skill.")


if __name__ == "__main__":
    main()
