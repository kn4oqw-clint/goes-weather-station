"""Cache the 2-D column products a segmentation actually needs.

Downloading and gridding a NEXRAD volume costs ~25-35 s; comparing several
segmentation algorithms over the same volumes should not pay that once per
algorithm. Everything the admission gates and the cell attributes need reduces
to three per-column fields, so the cache is ~3 MB per volume rather than the
full 3-D grid:

``colmax``      column-maximum reflectivity -- the composite that 2-D
                segmentation runs on.
``etop_km``     height of the highest level at or above ``continuity_dbz``.
``nlev_seed``   how many levels reach ``seed_dbz`` -- the vertical-continuity
                gate that rejects anomalous propagation.

Per-object gates then take the max of ``etop_km`` and ``nlev_seed`` over the
object's footprint, which is what the earlier enhanced-watershed trial did and
keeps the comparison honest across algorithms.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import numpy as np

BUCKET = "unidata-nexrad-level2"
CACHE = os.environ.get("VOLCACHE", "/var/tmp/volcache")


def _s3():
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    return boto3.client("s3", config=Config(signature_version=UNSIGNED,
                                            max_pool_connections=32),
                        region_name="us-east-1")


def volume_keys(site, t0, t1):
    s3 = _s3()
    keys = []
    day = t0.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= t1:
        tok = None
        while True:
            kw = {"Bucket": BUCKET, "Prefix": f"{day:%Y/%m/%d}/{site}/"}
            if tok:
                kw["ContinuationToken"] = tok
            r = s3.list_objects_v2(**kw)
            for o in r.get("Contents", []):
                k = o["Key"]
                if k.endswith("_MDM"):
                    continue
                b = k.rsplit("/", 1)[-1]
                try:
                    ts = datetime.strptime(b[4:19], "%Y%m%d_%H%M%S").replace(
                        tzinfo=timezone.utc)
                except ValueError:
                    continue
                if t0 <= ts <= t1:
                    keys.append((ts, k))
            tok = r.get("NextContinuationToken")
            if not tok:
                break
        day += timedelta(days=1)
    return sorted(keys)


def get_grid(site, ts, key, grid_datatree, xradar, GriddedVolume,
             tmp="/mnt/ingest/vg.ar2v"):
    """Return the FULL gridded volume, caching the 3-D reflectivity.

    ~40 MB per volume uncompressed but mostly NaN, so it compresses hard. Worth
    the disk: gridding costs ~30 s per volume, which makes iterating on a
    tracker over a 30-volume case set a half-hour round trip otherwise.
    """
    from datetime import datetime as _dt
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{site}_{ts:%Y%m%d_%H%M%S}_grid.npz")
    if os.path.exists(path):
        d = np.load(path, allow_pickle=False)
        return GriddedVolume(
            site=site,
            valid_time=_dt.fromtimestamp(float(d["epoch"]), timezone.utc),
            reflectivity=d["refl"], x=d["x"], y=d["y"], z=d["z"],
            lat0=float(d["lat0"]), lon0=float(d["lon0"])), True

    s3 = _s3()
    s3.download_file(BUCKET, key, tmp)
    try:
        dtree = xradar.io.open_nexradlevel2_datatree(tmp).load()
        vol, _ = grid_datatree(dtree, site)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    np.savez_compressed(
        path, refl=vol.reflectivity.astype(np.float32),
        x=vol.x, y=vol.y, z=vol.z,
        lat0=np.float64(vol.lat0), lon0=np.float64(vol.lon0),
        epoch=np.float64(vol.valid_time.timestamp()))
    return vol, False


def _derive(vol, params):
    refl = vol.reflectivity
    filled = np.where(np.isnan(refl), -np.inf, refl)
    colmax = filled.max(axis=0)
    colmax = np.where(np.isneginf(colmax), np.nan, colmax).astype(np.float32)

    zi = np.arange(refl.shape[0], dtype=np.float32)[:, None, None]
    top_ok = filled >= params.continuity_dbz
    etop_idx = np.where(top_ok.any(axis=0), np.where(top_ok, zi, -1).max(axis=0), -1)
    etop_km = np.where(etop_idx >= 0,
                       np.take(vol.z, np.clip(etop_idx.astype(int), 0, None)) / 1000.0,
                       np.nan).astype(np.float32)
    nlev_seed = (filled >= params.seed_dbz).sum(axis=0).astype(np.int16)
    return colmax, etop_km, nlev_seed


def get_volume(site, ts, key, params, grid_datatree, xradar,
               tmp="/mnt/ingest/vc.ar2v"):
    """Return the cached column products for one volume, gridding on a miss."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{site}_{ts:%Y%m%d_%H%M%S}.npz")
    if os.path.exists(path):
        d = np.load(path, allow_pickle=False)
        return {k: d[k] for k in d.files} | {"cached": True}

    s3 = _s3()
    s3.download_file(BUCKET, key, tmp)
    try:
        dt = xradar.io.open_nexradlevel2_datatree(tmp).load()
        vol, _ = grid_datatree(dt, site)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    colmax, etop_km, nlev_seed = _derive(vol, params)
    out = {"colmax": colmax, "etop_km": etop_km, "nlev_seed": nlev_seed,
           "x": vol.x.astype(np.float64), "y": vol.y.astype(np.float64),
           "lat0": np.float64(vol.lat0), "lon0": np.float64(vol.lon0),
           "dx_km": np.float64(vol.dx_km),
           "epoch": np.float64(vol.valid_time.timestamp())}
    np.savez_compressed(path, **out)
    out["cached"] = False
    return out
