#!/usr/bin/env python3
"""Quantify the max-binning bias before changing the gridder.

Zhang et al. (2005) tested nearest-neighbour / maximum / weighted-mean binning
and found maximum "would result in overestimation biases from the VPR issue"
and from radar calibration problems. The bias propagates into VIL and MESH,
which integrate calibrated reflectivity above the melting level.

That is a reason to *measure*, not a reason to assume. This grids the same
sweeps both ways and reports the difference, so the change is justified by a
number from this radar and this gridder rather than by citation.

Averaging is done in LINEAR Z (mm^6 m^-3), not in dBZ. dBZ is logarithmic, so a
dBZ-space mean is not the mean of the physical quantity VIL integrates -- it
would understate exactly the strong returns that matter.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# SCIT_SRC LAST so it lands at sys.path[0] and wins. The script dir
# also contains a scit/ tree, and inserting it afterwards would shadow
# SCIT_SRC entirely -- which silently tested the wrong copy once.
sys.path.insert(0, os.environ.get("SCIT_SRC", "/app"))
warnings.filterwarnings("ignore")

import xradar                                                    # noqa: E402
from scit.grid import IR, REFL_FIELD, fill_vertical_gaps         # noqa: E402
from volcache import BUCKET, _s3, volume_keys                    # noqa: E402


def grid_both(dt, h_m=1000.0, v_m=500.0, range_m=150_000.0, top_m=15_000.0):
    """Return (max_binned, mean_binned) reflectivity grids in dBZ."""
    nx = ny = int(2 * range_m / h_m) + 1
    nz = int(top_m / v_m) + 1
    N = nz * ny * nx
    gmax = np.full((nz, ny, nx), -np.inf, dtype=np.float32)
    zsum = np.zeros(N, dtype=np.float64)     # linear Z accumulator
    zcnt = np.zeros(N, dtype=np.int64)

    for sw in [k for k in dt.children if k.startswith("sweep")]:
        ds = dt[sw].ds
        if REFL_FIELD not in ds:
            continue
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
        np.maximum.at(gmax, (iz, iy, ix), vals)
        flat = (iz * ny + iy) * nx + ix
        zsum += np.bincount(flat, weights=np.power(10.0, vals / 10.0),
                            minlength=N)
        zcnt += np.bincount(flat, minlength=N)

    gmax[np.isneginf(gmax)] = np.nan
    with np.errstate(divide="ignore", invalid="ignore"):
        mean_lin = np.where(zcnt > 0, zsum / np.maximum(zcnt, 1), np.nan)
        gmean = (10.0 * np.log10(mean_lin)).astype(np.float32)
    gmean = gmean.reshape(nz, ny, nx)
    return fill_vertical_gaps(gmax), fill_vertical_gaps(gmean), zcnt.reshape(nz, ny, nx)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="/tmp/cases.json")
    ap.add_argument("--vols", type=int, default=2)
    args = ap.parse_args()
    s3 = _s3()

    print(f"{'case':<10}{'time':<8}{'cells':>10}{'multi%':>8}"
          f"{'mean dB':>9}{'p95 dB':>8}{'max dB':>8}{'>=40 mean':>11}{'sec':>6}")
    print("-" * 78)
    allbias, allbias40 = [], []
    for case in json.load(open(args.cases)):
        site = case["sites"][0]
        tag = f"{case['wfo']}-{case['etn']}"
        t0 = datetime.strptime(case["issued"], "%Y%m%d%H%M").replace(
            tzinfo=timezone.utc)
        t1 = datetime.strptime(case["expired"], "%Y%m%d%H%M").replace(
            tzinfo=timezone.utc)
        vl = volume_keys(site, t0, t1)[:args.vols]
        for ts, key in vl:
            tmp = "/mnt/ingest/bias.ar2v"
            t_a = time.time()
            try:
                s3.download_file(BUCKET, key, tmp)
                dt = xradar.io.open_nexradlevel2_datatree(tmp).load()
                gmax, gmean, cnt = grid_both(dt)
            except Exception as e:
                print(f"{tag:<10}{ts:%H:%M}  {type(e).__name__}: {e}")
                continue
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

            ok = np.isfinite(gmax) & np.isfinite(gmean)
            d = (gmax - gmean)[ok]
            multi = float(np.mean(cnt[ok] > 1)) * 100.0
            strong = ok & (gmax >= 40.0)
            d40 = (gmax - gmean)[strong]
            allbias.append(d)
            if d40.size:
                allbias40.append(d40)
            print(f"{tag:<10}{ts:%H:%M}  {d.size:>10}{multi:>8.1f}"
                  f"{d.mean():>9.2f}{np.percentile(d, 95):>8.2f}{d.max():>8.2f}"
                  f"{(d40.mean() if d40.size else float('nan')):>11.2f}"
                  f"{time.time()-t_a:>6.0f}")

    if allbias:
        d = np.concatenate(allbias)
        print("-" * 78)
        print(f"ALL CELLS   n={d.size}  mean {d.mean():.2f} dB  "
              f"median {np.median(d):.2f}  p95 {np.percentile(d,95):.2f}  "
              f"max {d.max():.2f}")
        if allbias40:
            d4 = np.concatenate(allbias40)
            print(f">=40 dBZ    n={d4.size}  mean {d4.mean():.2f} dB  "
                  f"median {np.median(d4):.2f}  p95 {np.percentile(d4,95):.2f}  "
                  f"max {d4.max():.2f}")
        print("\nmax - linear-mean, in dB. This is the overestimation that max")
        print("binning propagates into VIL and MESH.")


if __name__ == "__main__":
    main()
