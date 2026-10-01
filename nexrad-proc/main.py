#!/usr/bin/env python3
"""
scit-core — storm cell identification and tracking.

    Redis stream `nexrad:zarr` (consumer group)
        -> fetch the volume's Zarr from MinIO
        -> grid polar -> Cartesian            (scit.grid, no pyart)
        -> identify()                          (detection_v2, envelope-based)
        -> Tracker.update()                    (per-site, greedy NN)
        -> persist cells + envelopes to PostGIS
        -> announce on `nexrad:cells` for the engine cylinders
        -> XACK

WHY THIS EXISTS AND WHAT IT REPLACES
------------------------------------
The previous SCIT tracked essentially every high-dBZ gate, which is how the
system ended up emitting **tornado probabilities for cells under a kilometre
tall** — and why it was taken offline. `detection_v2` fixes that at the right
layer: a candidate must clear structural admission gates *before it becomes a
cell at all*, so no downstream cylinder can ever be handed one.

Demonstrated on a real clear-air KEVX volume during the build:

    seed 40 dBZ (default)      ->  0 cells
    seed 25 dBZ                ->  0 cells   (gates still reject)
    seed 25 dBZ, GATES OFF     -> 39 cells   top 0.0-1.5 km, 1-2 levels

Those 39 are exactly the shallow returns that broke the old system. Gate at
identification, never at scoring.

TRACKING IS PER SITE
--------------------
A `Tracker` is stateful and greedy-nearest-neighbour in radar-relative metres,
so mixing sites in one tracker would associate storms across two different
coordinate origins. Cross-site association (the same physical storm seen by
both KMOB and KEVX) belongs ABOVE this stage and is not done here.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import time
from datetime import datetime, timezone
from threading import Event

import boto3
import numpy as np
import psycopg2
import psycopg2.extras
import redis
import xarray as xr
import zarr
from botocore.client import Config as BotoConfig
from prometheus_client import Counter, Gauge, Histogram, start_http_server
from shapely.geometry import mapping

from scit.grid import grid_datatree
from scit.hierarchy import HierarchyParams, StormHierarchy
from scit.params import DetectionParams
from scit.tobac_detect import detect_volume

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
IN_STREAM = os.environ.get("REDIS_IN_STREAM", "nexrad:zarr")
OUT_STREAM = os.environ.get("REDIS_OUT_STREAM", "nexrad:cells")
GROUP = os.environ.get("REDIS_GROUP", "scit")
CONSUMER = os.environ.get("CONSUMER_NAME", socket.gethostname())
CLAIM_MIN_IDLE_MS = int(os.environ.get("CLAIM_MIN_IDLE_MS", "600000"))
# A volume is retried this many times before being treated as poison. Retries
# are paced by CLAIM_MIN_IDLE_MS, so 3 deliveries is ~20 min of grace before a
# corrupt volume is dropped -- long enough to ride out a MinIO outage.
MAX_DELIVERIES = int(os.environ.get("SCIT_MAX_DELIVERIES", "3"))
DLQ_STREAM = os.environ.get("REDIS_DLQ_STREAM", "nexrad:cells:dead")

MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://10.10.0.10")
MINIO_KEY = os.environ.get("MINIO_ACCESS_KEY", "")
MINIO_SECRET = os.environ.get("MINIO_SECRET_KEY", "")

PG_DSN = os.environ.get("SCIT_PG_DSN", "")
SCRATCH = os.environ.get("SCRATCH_DIR", "/mnt/ingest")
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9092"))

GRID_H_M = float(os.environ.get("SCIT_GRID_H_M", "1000"))
GRID_V_M = float(os.environ.get("SCIT_GRID_V_M", "500"))
GRID_RANGE_M = float(os.environ.get("SCIT_GRID_RANGE_M", "150000"))
GRID_TOP_M = float(os.environ.get("SCIT_GRID_TOP_M", "15000"))

PARAMS = DetectionParams.from_env(os.environ)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("scit")

_BT = (0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60)
h_fetch = Histogram("scit_fetch_seconds", "MinIO fetch + open", ["site"], buckets=_BT)
h_grid = Histogram("scit_grid_seconds", "Polar -> Cartesian grid", ["site"], buckets=_BT)
h_identify = Histogram("scit_identify_seconds", "Cell identification", ["site"], buckets=_BT)
h_persist = Histogram("scit_persist_seconds", "DB write", ["site"], buckets=_BT)
h_total = Histogram("scit_total_seconds", "Total per volume", ["site"], buckets=_BT)

h_cells = Histogram("scit_cells_per_volume", "Admitted cells per volume", ["site"],
                    buckets=(0, 1, 2, 5, 10, 20, 50, 100, 200))
h_gates = Histogram("scit_gates_binned", "Radar gates binned into the grid", ["site"],
                    buckets=(1e5, 5e5, 1e6, 2e6, 5e6, 1e7))
h_maxdbz = Histogram("scit_volume_max_dbz", "Composite max reflectivity", ["site"],
                     buckets=(5, 15, 25, 30, 35, 40, 45, 50, 55, 60, 70))
h_top = Histogram("scit_cell_echo_top_km", "Echo top of admitted cells", ["site"],
                  buckets=(3, 5, 7, 9, 11, 13, 15))
h_area = Histogram("scit_cell_area_km2", "Footprint area of admitted cells", ["site"],
                   buckets=(4, 10, 25, 50, 100, 250, 500, 1000))

c_volumes = Counter("scit_volumes_total", "Volumes processed", ["site", "status"])
c_cells = Counter("scit_cells_total", "Cells admitted", ["site"])
c_tracks = Counter("scit_tracks_started_total", "New tracks opened", ["site"])
h_families = Histogram("scit_families_per_volume", "Storm families per volume",
                       ["site"], buckets=(0, 1, 2, 3, 5, 8, 12, 20, 40))
c_errors = Counter("scit_errors_total", "Errors", ["stage"])
c_poison = Counter("scit_poison_total", "Volumes dropped to the dead-letter "
                   "stream after exhausting retries", ["site"])
g_active = Gauge("scit_active_tracks", "Live tracks in the tracker", ["site"])
g_active_fam = Gauge("scit_active_families", "Live storm families", ["site"])
g_pending = Gauge("scit_pending", "Unacked entries in the consumer group")
g_inflight = Gauge("scit_inflight", "Volumes being processed")
g_hash = Gauge("scit_settings_hash_info", "Detection settings hash (as a label)",
               ["settings_hash"])

_s3 = boto3.client("s3", endpoint_url=MINIO_ENDPOINT,
                   aws_access_key_id=MINIO_KEY, aws_secret_access_key=MINIO_SECRET,
                   config=BotoConfig(signature_version="s3v4",
                                     s3={"addressing_style": "path"},
                                     retries={"max_attempts": 3, "mode": "standard"}),
                   region_name="us-east-1")
_stop = Event()
_hier: dict[str, StormHierarchy] = {}       # per-site — see module docstring
HP = HierarchyParams(
    window=int(os.environ.get("SCIT_LINK_WINDOW", "12")),
    v_max_ms=float(os.environ.get("SCIT_V_MAX_MS", "30")),
    memory=int(os.environ.get("SCIT_LINK_MEMORY", "1")),
    family_distance_m=float(os.environ.get("SCIT_FAMILY_DISTANCE_M", "25000")),
)

# Envelopes are MultiPolygon-capable now: the exact footprint of a storm with a
# detached fragment, or a family whose members are not contiguous, is genuinely
# multi-part. Geometry(Geometry,4326) accepts both rather than silently failing
# the way geography(Polygon) would.
DDL = """
CREATE TABLE IF NOT EXISTS scit_cells (
    site            text        NOT NULL,
    valid_time      timestamptz NOT NULL,
    cell_id         int         NOT NULL,
    track_id        int         NOT NULL,
    family_id       int,
    split_from      int,        -- the cell this one broke off; its history is
                                -- inherited, so age_min and the trend columns
                                -- describe the parent storm, not a fresh start
    seed_lon        double precision NOT NULL,
    seed_lat        double precision NOT NULL,
    max_dbz         real,
    area_km2        real,
    echo_top_km     real,
    base_km         real,
    depth_km        real,
    n_levels        int,
    age_min         real,
    d_area_km2_min  real,
    d_dbz_min       real,
    d_top_km_min    real,
    vcp             int,
    envelope        geography(Geometry,4326),
    settings_hash   text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site, valid_time, cell_id)
);
CREATE INDEX IF NOT EXISTS scit_cells_time_idx  ON scit_cells (valid_time DESC);
CREATE INDEX IF NOT EXISTS scit_cells_track_idx ON scit_cells (site, track_id, valid_time DESC);
CREATE INDEX IF NOT EXISTS scit_cells_geom_idx  ON scit_cells USING gist (envelope);
-- NOTE: the index on family_id lives in MIGRATE, not here. On an existing
-- database CREATE TABLE IF NOT EXISTS is a no-op, so the column does not exist
-- yet at this point and creating its index here aborts the whole statement.

CREATE TABLE IF NOT EXISTS scit_families (
    site            text        NOT NULL,
    valid_time      timestamptz NOT NULL,
    family_id       int         NOT NULL,
    split_from      int,
    n_cells         int         NOT NULL,
    cell_ids        int[],
    centroid_lon    double precision NOT NULL,
    centroid_lat    double precision NOT NULL,
    max_dbz         real,
    area_km2        real,
    echo_top_km     real,
    age_min         real,
    d_area_km2_min  real,
    d_dbz_min       real,
    d_cells_min     real,
    vcp             int,
    envelope        geography(Geometry,4326),
    settings_hash   text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site, valid_time, family_id)
);
CREATE INDEX IF NOT EXISTS scit_fam_time_idx ON scit_families (valid_time DESC);
CREATE INDEX IF NOT EXISTS scit_fam_id_idx   ON scit_families (site, family_id, valid_time DESC);
CREATE INDEX IF NOT EXISTS scit_fam_geom_idx ON scit_families USING gist (envelope);
"""


# Idempotent migration for a table created before the two-level model. The DDL
# above only runs for a fresh database; CREATE TABLE IF NOT EXISTS will not add
# a column to a table that already exists.
#
# The envelope widening is safe in this direction: every Polygon is a valid
# Geometry, so existing rows convert without rewriting, while a family of
# non-contiguous cores (a genuine MultiPolygon) can finally be stored. Going
# back the other way would not be safe.
MIGRATE = """
ALTER TABLE scit_cells ADD COLUMN IF NOT EXISTS family_id      int;
ALTER TABLE scit_cells ADD COLUMN IF NOT EXISTS split_from     int;
ALTER TABLE scit_cells ADD COLUMN IF NOT EXISTS age_min        real;
ALTER TABLE scit_cells ADD COLUMN IF NOT EXISTS d_area_km2_min real;
ALTER TABLE scit_cells ADD COLUMN IF NOT EXISTS d_dbz_min      real;
ALTER TABLE scit_cells ADD COLUMN IF NOT EXISTS d_top_km_min   real;
ALTER TABLE scit_cells ALTER COLUMN envelope TYPE geography(Geometry,4326);
CREATE INDEX IF NOT EXISTS scit_cells_fam_idx
    ON scit_cells (site, family_id, valid_time DESC);
"""


def ensure_schema():
    if not PG_DSN:
        log.warning("SCIT_PG_DSN unset — running WITHOUT persistence")
        return
    with psycopg2.connect(PG_DSN) as conn, conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute(MIGRATE)
        # Timescale hypertable if available — the comparator needs per-track
        # history, which is a time-series access pattern.
        try:
            cur.execute("SELECT create_hypertable('scit_cells','valid_time',"
                        "if_not_exists=>TRUE, migrate_data=>TRUE);")
        except Exception as e:
            conn.rollback()
            log.info(f"hypertable not created ({e}); plain table is fine")
        conn.commit()
    log.info("scit_cells schema ready")


def _announce(d: dict, _uid: str) -> dict:
    """JSON-safe view for the cylinders. Drops the numpy/shapely working state
    (`envelope_xy`, `_det`) that only the detector itself needs."""
    out = {k: v for k, v in d.items()
           if k not in ("envelope_xy", "_det", "envelope")}
    out["valid_time"] = d["valid_time"].isoformat()
    out["envelope"] = mapping(d["envelope"])
    out["trend"] = {k: (None if isinstance(v, float) and np.isnan(v) else v)
                    for k, v in d["trend"].items()}
    return out


def _nn(v):
    """NaN -> None. A trend is NaN until a node has two observations, and
    psycopg2 would otherwise write NaN into a real column."""
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else v


def persist(cells, families, vcp):
    if not PG_DSN or not cells:
        return
    crows = []
    for i, c in enumerate(cells, 1):
        tr = c["trend"]
        crows.append((
            c["site"], c["valid_time"], i, c["cell_uid"], c["family_uid"],
            c["split_from"],
            c["seed_lon"], c["seed_lat"], c["max_dbz"], c["area_km2"],
            c["echo_top_km"], c["base_km"], c["depth_km"], c["n_levels"],
            _nn(c["age_min"]), _nn(tr["area_km2_per_min"]), _nn(tr["dbz_per_min"]),
            _nn(tr["top_km_per_min"]), vcp,
            json.dumps(mapping(c["envelope"])), PARAMS.settings_hash))
    frows = []
    for f in families:
        tr = f["trend"]
        frows.append((
            f["site"], f["valid_time"], f["family_uid"], f["split_from"],
            f["n_cells"],
            f["cell_uids"], f["centroid_lon"], f["centroid_lat"], f["max_dbz"],
            f["area_km2"], f["echo_top_km"], _nn(f["age_min"]),
            _nn(tr["area_km2_per_min"]), _nn(tr["dbz_per_min"]),
            _nn(tr["cells_per_min"]), vcp,
            json.dumps(mapping(f["envelope"])), PARAMS.settings_hash))

    geo = "ST_GeogFromText(ST_AsText(ST_GeomFromGeoJSON(%s)))"
    with psycopg2.connect(PG_DSN) as conn, conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO scit_cells (site, valid_time, cell_id, track_id,
                family_id, split_from, seed_lon, seed_lat, max_dbz, area_km2,
                echo_top_km, base_km, depth_km, n_levels, age_min,
                d_area_km2_min, d_dbz_min, d_top_km_min, vcp, envelope,
                settings_hash)
            VALUES %s
            ON CONFLICT (site, valid_time, cell_id) DO NOTHING
        """, crows, template="(" + ",".join(["%s"] * 19) + f",{geo},%s)")
        if frows:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO scit_families (site, valid_time, family_id,
                    split_from, n_cells, cell_ids, centroid_lon, centroid_lat,
                    max_dbz, area_km2, echo_top_km, age_min, d_area_km2_min,
                    d_dbz_min, d_cells_min, vcp, envelope, settings_hash)
                VALUES %s
                ON CONFLICT (site, valid_time, family_id) DO NOTHING
            """, frows, template="(" + ",".join(["%s"] * 16) + f",{geo},%s)")
        conn.commit()


def process(r, unit) -> bool:
    site = unit["site"]
    tmp = os.path.join(SCRATCH, f"scit_{site}_{unit['volume_epoch']}.zip")
    g_inflight.inc()
    t_start = time.perf_counter()
    try:
        try:
            t0 = time.perf_counter()
            _s3.download_file(unit["bucket"], unit["key"], tmp)
            dt = xr.open_datatree(zarr.storage.ZipStore(tmp, mode="r"),
                                  engine="zarr", consolidated=False).load()
            t_fetch = time.perf_counter() - t0
        except Exception as e:
            c_errors.labels(stage="fetch").inc()
            raise RuntimeError(f"fetch: {e}") from e
        h_fetch.labels(site=site).observe(t_fetch)

        try:
            t0 = time.perf_counter()
            vol, gates = grid_datatree(dt, site, h_m=GRID_H_M, v_m=GRID_V_M,
                                       range_m=GRID_RANGE_M, top_m=GRID_TOP_M)
            t_grid = time.perf_counter() - t0
        except Exception as e:
            c_errors.labels(stage="grid").inc()
            raise RuntimeError(f"grid: {e}") from e
        h_grid.labels(site=site).observe(t_grid)
        h_gates.labels(site=site).observe(gates)
        comp = vol.composite_reflectivity()
        maxdbz = float(np.nanmax(comp)) if np.isfinite(comp).any() else float("nan")
        if np.isfinite(maxdbz):
            h_maxdbz.labels(site=site).observe(maxdbz)

        try:
            t0 = time.perf_counter()
            hier = _hier.setdefault(site, StormHierarchy(site, PARAMS, HP))
            before = hier._next_cell
            dets, feats = detect_volume(vol, PARAMS)
            cells, families = hier.update(vol, dets, feats)
            t_id = time.perf_counter() - t0
        except Exception as e:
            c_errors.labels(stage="identify").inc()
            raise RuntimeError(f"identify: {e}") from e
        h_identify.labels(site=site).observe(t_id)
        h_cells.labels(site=site).observe(len(cells))
        h_families.labels(site=site).observe(len(families))
        c_cells.labels(site=site).inc(len(cells))
        c_tracks.labels(site=site).inc(max(0, hier._next_cell - before))
        g_active.labels(site=site).set(len(hier.cells))
        g_active_fam.labels(site=site).set(len(hier.families))
        for c in cells:
            h_top.labels(site=site).observe(c["echo_top_km"])
            h_area.labels(site=site).observe(c["area_km2"])

        try:
            t0 = time.perf_counter()
            persist(cells, families, unit.get("vcp"))
            t_db = time.perf_counter() - t0
        except Exception as e:
            c_errors.labels(stage="persist").inc()
            raise RuntimeError(f"persist: {e}") from e
        h_persist.labels(site=site).observe(t_db)

        try:
            r.xadd(OUT_STREAM, {"unit": json.dumps({
                "site": site, "volume_utc": unit["volume_utc"],
                "volume_epoch": unit["volume_epoch"],
                "cell_count": len(cells), "family_count": len(families),
                "max_dbz": None if np.isnan(maxdbz) else maxdbz,
                "settings_hash": PARAMS.settings_hash,
                "cells": [_announce(c, "cell_uid") for c in cells],
                "families": [_announce(f, "family_uid") for f in families],
            })}, maxlen=5000, approximate=True)
        except Exception as e:
            c_errors.labels(stage="announce").inc()
            log.error(f"announce failed (cells ARE persisted): {e}")

        t_tot = time.perf_counter() - t_start
        h_total.labels(site=site).observe(t_tot)
        c_volumes.labels(site=site, status="ok").inc()
        top = max((c["max_dbz"] for c in cells), default=None)
        log.info(f"OK {site} {unit['volume_utc']} vcp={unit.get('vcp')} "
                 f"| fetch {t_fetch:5.2f}s | grid {t_grid:5.2f}s {gates/1e6:.1f}Mgates "
                 f"| ident {t_id:5.2f}s -> {len(cells)} cells "
                 f"in {len(families)} families"
                 f"{f' (max {top:.1f}dBZ)' if top else ''} "
                 f"| db {t_db:5.2f}s | tot {t_tot:5.2f}s | comp_max {maxdbz:.1f}dBZ")
        return True
    except Exception as e:
        c_volumes.labels(site=site, status="error").inc()
        log.error(f"FAIL {site} {unit.get('volume_utc')}: {e}")
        return False
    finally:
        g_inflight.dec()
        try:
            os.remove(tmp)
        except OSError:
            pass


def _times_delivered(r, msg_id) -> int:
    """How many times Redis has handed this entry out. 1 on the first try."""
    try:
        info = r.xpending_range(IN_STREAM, GROUP, min=msg_id, max=msg_id, count=1)
        return int(info[0]["times_delivered"]) if info else 1
    except Exception:
        return 1


def handle(r, msg_id, fields):
    try:
        unit = json.loads(fields["unit"])
    except Exception:
        c_errors.labels(stage="parse").inc()
        r.xack(IN_STREAM, GROUP, msg_id)
        return
    if process(r, unit):
        r.xack(IN_STREAM, GROUP, msg_id)
        return

    # POISON-MESSAGE HANDLING. A failure used to mean "do not ack", so the entry
    # stayed pending, xautoclaim re-delivered it, and it failed again -- forever.
    # One truncated volume (xradar raises IndexError on a short file) would hold
    # a consumer slot for good and fill the log with the same traceback.
    #
    # Bounded retries separate the two failure kinds without having to classify
    # them: a transient fault (a MinIO blip) succeeds within MAX_DELIVERIES,
    # a permanently corrupt volume never will and is dropped to the dead-letter
    # stream so the group can move on. The unit is preserved there rather than
    # discarded, so a poison volume can still be inspected afterwards.
    n = _times_delivered(r, msg_id)
    if n < MAX_DELIVERIES:
        log.warning(f"retry {n}/{MAX_DELIVERIES} for {unit.get('site')} "
                    f"{unit.get('volume_utc')} (left pending)")
        return
    site = unit.get("site", "?")
    c_poison.labels(site=site).inc()
    try:
        r.xadd(DLQ_STREAM, {"unit": json.dumps(unit), "deliveries": n,
                            "dropped_at": datetime.now(timezone.utc).isoformat()},
               maxlen=1000, approximate=True)
    except Exception as e:
        log.error(f"dead-letter write failed for {msg_id}: {e}")
    r.xack(IN_STREAM, GROUP, msg_id)
    log.error(f"POISON {site} {unit.get('volume_utc')} dropped after {n} "
              f"deliveries -> {DLQ_STREAM}")


def main():
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _stop.set())
    os.makedirs(SCRATCH, exist_ok=True)
    start_http_server(METRICS_PORT)
    g_hash.labels(settings_hash=PARAMS.settings_hash).set(1)

    ensure_schema()

    r = redis.from_url(REDIS_URL, decode_responses=True)
    for _ in range(30):
        try:
            r.ping()
            break
        except Exception:
            time.sleep(2)
    try:
        r.xgroup_create(IN_STREAM, GROUP, id="0", mkstream=True)
        log.info(f"created consumer group {GROUP}")
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise

    log.info(f"scit-core up: consumer={CONSUMER} in={IN_STREAM} out={OUT_STREAM} "
             f"grid={GRID_H_M:.0f}m/{GRID_V_M:.0f}m r={GRID_RANGE_M/1000:.0f}km "
             f"seed={PARAMS.seed_dbz}dBZ echo_top_min={PARAMS.echo_top_min_km}km "
             f"levels={PARAMS.continuity_levels} hash={PARAMS.settings_hash} "
             f"(metrics :{METRICS_PORT})")

    while not _stop.is_set():
        try:
            claimed = r.xautoclaim(IN_STREAM, GROUP, CONSUMER,
                                   min_idle_time=CLAIM_MIN_IDLE_MS, count=1)
            for msg_id, fields in (claimed[1] or []):
                log.warning(f"reclaimed stale entry {msg_id}")
                handle(r, msg_id, fields)

            resp = r.xreadgroup(GROUP, CONSUMER, {IN_STREAM: ">"}, count=1, block=5000)
            for _s, entries in (resp or []):
                for msg_id, fields in entries:
                    handle(r, msg_id, fields)
            try:
                p = r.xpending(IN_STREAM, GROUP)
                g_pending.set(p.get("pending", 0) if isinstance(p, dict) else 0)
            except Exception:
                pass
        except Exception as e:
            c_errors.labels(stage="loop").inc()
            log.error(f"loop error: {e}")
            _stop.wait(5)

    log.info("shutting down")


if __name__ == "__main__":
    main()
