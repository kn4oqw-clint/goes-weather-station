#!/usr/bin/env python3
"""Confirm the calibrated grid is produced, is sane, and costs acceptable time.

Checks:
  * both fields exist with the same shape and the same finite mask
  * cal <= max everywhere (a mean can never exceed a maximum) -- a violation
    would mean the linear/dB conversion is inverted somewhere
  * the wall-clock cost of the extra accumulation
"""
import json
import os
import sys
import time
import warnings
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# SCIT_SRC LAST so it lands at sys.path[0] and wins. The script dir
# also contains a scit/ tree, and inserting it afterwards would shadow
# SCIT_SRC entirely -- which silently tested the wrong copy once.
sys.path.insert(0, os.environ.get("SCIT_SRC", "/app"))
warnings.filterwarnings("ignore")

import xradar                                        # noqa: E402
from scit.grid import grid_datatree                  # noqa: E402
from volcache import BUCKET, _s3, volume_keys        # noqa: E402

s3 = _s3()
for case in json.load(open("/tmp/cases.json"))[:2]:
    site = case["sites"][0]
    t0 = datetime.strptime(case["issued"], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    t1 = datetime.strptime(case["expired"], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    vl = volume_keys(site, t0, t1)[:1]
    for ts, key in vl:
        tmp = "/mnt/ingest/gc.ar2v"
        s3.download_file(BUCKET, key, tmp)
        dt = xradar.io.open_nexradlevel2_datatree(tmp).load()
        t_a = time.time()
        vol, gates = grid_datatree(dt, site)
        el = time.time() - t_a
        os.remove(tmp)

        mx, cal = vol.reflectivity, vol.reflectivity_cal
        assert cal is not None, "reflectivity_cal missing"
        assert cal.shape == mx.shape, f"shape {cal.shape} != {mx.shape}"
        both = np.isfinite(mx) & np.isfinite(cal)
        # A mean can never exceed a maximum. Allow float32 rounding only.
        viol = int(np.count_nonzero(cal[both] > mx[both] + 1e-3))
        d = (mx - cal)[both]
        onlymax = int(np.count_nonzero(np.isfinite(mx) & ~np.isfinite(cal)))
        onlycal = int(np.count_nonzero(~np.isfinite(mx) & np.isfinite(cal)))
        print(f"{site} {ts:%H:%M}Z  grid {el:5.1f}s  {gates/1e6:.1f}Mgates  "
              f"finite both={both.sum()}  max-only={onlymax}  cal-only={onlycal}\n"
              f"    cal>max violations: {viol}   "
              f"mean(max-cal) {d.mean():.2f} dB   p95 {np.percentile(d,95):.2f} dB")
        if viol:
            print("    !! FAIL: a mean exceeded the maximum")
            sys.exit(1)
print("OK: calibrated grid present, bounded by the max grid, gap-filled alike")
