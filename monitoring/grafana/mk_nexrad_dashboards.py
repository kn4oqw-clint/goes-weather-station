#!/usr/bin/env python3
"""Build and publish the NEXRAD Pipeline Grafana dashboards.

Usage:  GRAFANA_TOKEN=glsa_... python3 mk_nexrad_dashboards.py

Idempotent (overwrite: true) — re-running redeploys in place.
Add a cylinder by copying a dashboard dict and giving it a new uid.

One dashboard per stage so each engine cylinder can get its own page later
without any of them turning into a wall of unrelated panels.
"""
import json
import os
import urllib.request

G = "http://172.16.50.146:3000"
TOK = os.environ["GRAFANA_TOKEN"]   # service account token — never commit this
FOLDER = "nexrad-pipeline"
DS = {"type": "prometheus", "uid": "PBFA97CFB590B2093"}

_id = [0]


def nid():
    _id[0] += 1
    return _id[0]


def tgt(expr, legend=None, instant=False):
    t = {"datasource": DS, "expr": expr, "refId": chr(65 + (nid() % 26))}
    if legend:
        t["legendFormat"] = legend
    if instant:
        t["instant"] = True
    return t


def stat(title, x, y, w, h, targets, unit=None, desc=None, mappings=None,
         thresholds=None, color="thresholds", dec=None):
    fc = {"mode": color, "fixedColor": "text"}
    p = {
        "id": nid(), "type": "stat", "title": title,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "datasource": DS, "targets": targets,
        "description": desc or "",
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "orientation": "auto", "textMode": "auto", "colorMode": "value",
                    "graphMode": "area", "justifyMode": "auto"},
        "fieldConfig": {"defaults": {"color": fc, "mappings": mappings or [],
                                     "thresholds": thresholds or
                                     {"mode": "absolute", "steps": [{"color": "green", "value": None}]}},
                        "overrides": []},
    }
    if unit:
        p["fieldConfig"]["defaults"]["unit"] = unit
    if dec is not None:
        p["fieldConfig"]["defaults"]["decimals"] = dec
    return p


def ts(title, x, y, w, h, targets, unit=None, desc=None, stack=False, dec=None,
       fill=10, minv=None):
    p = {
        "id": nid(), "type": "timeseries", "title": title,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "datasource": DS, "targets": targets,
        "description": desc or "",
        "options": {"legend": {"displayMode": "list", "placement": "bottom",
                               "showLegend": True, "calcs": ["mean", "max"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "fieldConfig": {"defaults": {
            "custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": fill,
                       "showPoints": "never", "spanNulls": True,
                       "stacking": {"mode": "normal" if stack else "none"}},
            "color": {"mode": "palette-classic"},
            "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
        }, "overrides": []},
    }
    if unit:
        p["fieldConfig"]["defaults"]["unit"] = unit
    if dec is not None:
        p["fieldConfig"]["defaults"]["decimals"] = dec
    if minv is not None:
        p["fieldConfig"]["defaults"]["min"] = minv
    return p


def row(title, y):
    return {"id": nid(), "type": "row", "title": title, "collapsed": False,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}


def publish(dash):
    body = json.dumps({"dashboard": dash, "folderUid": FOLDER,
                       "overwrite": True, "message": "provisioned by claude"}).encode()
    req = urllib.request.Request(f"{G}/api/dashboards/db", data=body,
                                 headers={"Authorization": f"Bearer {TOK}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    print(f"  {dash['title']:34s} -> {out.get('status')}  {G}{out.get('url','')}")


# ===================== POLLER =====================
VCP_MAP = [{"type": "value", "options": {
    "31": {"text": "31 clear-air", "color": "blue"},
    "32": {"text": "32 clear-air", "color": "blue"},
    "35": {"text": "35 clear-air", "color": "blue"},
    "12": {"text": "12 PRECIP", "color": "orange"},
    "112": {"text": "112 PRECIP", "color": "orange"},
    "212": {"text": "212 PRECIP", "color": "red"},
    "215": {"text": "215 PRECIP", "color": "red"},
    "21": {"text": "21 PRECIP", "color": "orange"},
    "121": {"text": "121 PRECIP", "color": "orange"},
}}]

poller = {
    "uid": "nexrad-poller", "title": "NEXRAD — Poller", "tags": ["nexrad", "pipeline"],
    "timezone": "browser", "schemaVersion": 39, "refresh": "30s",
    "time": {"from": "now-6h", "to": "now"},
    "description": "S3 discovery of completed NEXRAD volumes. Reads the VCP from "
                   "each volume's ~2.4 kB -S chunk and drops clear-air volumes "
                   "before they are ever downloaded.",
    "panels": [
        row("Overview", 0),
        stat("Enqueued (1h)", 0, 1, 4, 4,
             [tgt("sum(increase(nexrad_poller_enqueued_total[1h]))", "enqueued")],
             desc="Precip volumes handed to the ingest queue."),
        stat("Clear-air skipped (1h)", 4, 1, 4, 4,
             [tgt("sum(increase(nexrad_poller_skipped_total{reason=\"clear_air\"}[1h]))", "skipped")],
             desc="Volumes dropped because the radar was in a clear-air VCP (31/32/35).",
             color="fixed"),
        stat("Bandwidth avoided (1h)", 8, 1, 4, 4,
             [tgt("sum(increase(nexrad_poller_bytes_skipped_total[1h]))", "saved")],
             unit="bytes",
             desc="Download bytes never spent, thanks to reading the VCP from the "
                  "2.4 kB -S chunk instead of the full ~9.4 MB volume."),
        stat("Queue depth", 12, 1, 4, 4,
             [tgt("nexrad_poller_stream_depth", "depth")],
             desc="Entries in the nexrad:volumes Redis stream."),
        stat("Current VCP", 16, 1, 8, 4,
             [tgt("nexrad_poller_current_vcp", "{{site}}")],
             mappings=VCP_MAP, color="fixed",
             desc="Volume Coverage Pattern per site. Blue = clear air (skipped), "
                  "orange/red = precipitation (ingested)."),

        row("Health", 5),
        ts("S3 calls per poll  — CANARY", 0, 6, 12, 7,
           [tgt("increase(nexrad_poller_s3_calls_total[10m]) / "
                "clamp_min(increase(nexrad_poller_polls_total[10m]), 1)", "{{site}}")],
           dec=1, minv=0,
           desc="THE canary. Design target is ~6 calls/poll/site. If this climbs, "
                "prefix selection has regressed toward enumerating every volume "
                "directory (400+ per site)."),
        ts("Volume lag", 12, 6, 12, 7,
           [tgt("nexrad_poller_volume_lag_seconds", "{{site}}")], unit="s",
           desc="Age of the newest enqueued volume at enqueue time — radar scan "
                "start to discovery."),

        row("Rates", 13),
        ts("Enqueued vs skipped", 0, 14, 12, 7,
           [tgt("rate(nexrad_poller_enqueued_total[5m])*300", "enqueued {{site}}"),
            tgt("rate(nexrad_poller_skipped_total[5m])*300", "skipped {{site}} ({{reason}})")],
           dec=2, minv=0, desc="Volumes per 5 min."),
        ts("Errors by stage", 12, 14, 12, 7,
           [tgt("rate(nexrad_poller_errors_total[5m])*300", "{{stage}}")],
           dec=2, minv=0, desc="Should be flat zero."),
    ],
}

# ===================== INGEST =====================
ingest = {
    "uid": "nexrad-ingest", "title": "NEXRAD — Ingest", "tags": ["nexrad", "pipeline"],
    "timezone": "browser", "schemaVersion": 39, "refresh": "30s",
    "time": {"from": "now-6h", "to": "now"},
    "description": "Downloads chunks, decodes with xradar, writes Zarr v3 (sharded) "
                   "into a ZipStore and uploads to MinIO. One object per volume.",
    "panels": [
        row("Overview", 0),
        stat("Volumes OK (1h)", 0, 1, 4, 4,
             [tgt("sum(increase(nexrad_ingest_volumes_total{status=\"ok\"}[1h]))", "ok")]),
        stat("Failures (1h)", 4, 1, 4, 4,
             [tgt("sum(increase(nexrad_ingest_volumes_total{status=\"error\"}[1h]))", "err")],
             thresholds={"mode": "absolute", "steps": [
                 {"color": "green", "value": None}, {"color": "red", "value": 1}]}),
        stat("Objects / volume  — CANARY", 8, 1, 5, 4,
             [tgt("sum(rate(nexrad_ingest_objects_per_volume_sum[1h])) / "
                  "clamp_min(sum(rate(nexrad_ingest_objects_per_volume_count[1h])),0.0001)", "obj")],
             dec=2,
             thresholds={"mode": "absolute", "steps": [
                 {"color": "green", "value": None}, {"color": "orange", "value": 2},
                 {"color": "red", "value": 50}]},
             desc="THE object-count canary — this is what sank the R820. Must stay "
                  "at 1 (ZipStore). A directory store would put it near 429."),
        stat("Mean time / volume", 13, 1, 5, 4,
             [tgt("sum(rate(nexrad_ingest_total_seconds_sum[1h])) / "
                  "clamp_min(sum(rate(nexrad_ingest_total_seconds_count[1h])),0.0001)", "s")],
             unit="s", dec=2),
        stat("Pending / in-flight", 18, 1, 6, 4,
             [tgt("nexrad_ingest_pending", "pending"),
              tgt("nexrad_ingest_inflight", "in-flight")],
             desc="Pending = unacked entries in the consumer group. Sustained "
                  "growth means work is failing and being reclaimed."),

        row("Speed", 5),
        ts("Stage timings (mean)", 0, 6, 12, 8,
           [tgt("sum(rate(nexrad_ingest_download_seconds_sum[10m])) / "
                "clamp_min(sum(rate(nexrad_ingest_download_seconds_count[10m])),0.0001)", "download"),
            tgt("sum(rate(nexrad_ingest_decode_seconds_sum[10m])) / "
                "clamp_min(sum(rate(nexrad_ingest_decode_seconds_count[10m])),0.0001)", "decode"),
            tgt("sum(rate(nexrad_ingest_zarr_write_seconds_sum[10m])) / "
                "clamp_min(sum(rate(nexrad_ingest_zarr_write_seconds_count[10m])),0.0001)", "zarr write"),
            tgt("sum(rate(nexrad_ingest_upload_seconds_sum[10m])) / "
                "clamp_min(sum(rate(nexrad_ingest_upload_seconds_count[10m])),0.0001)", "upload")],
           unit="s", dec=2, stack=True, minv=0,
           desc="Baseline: download 0.94s, decode 5.17s, zarr 5.56s, upload 0.32s "
                "-> ~12s total. Decode and Zarr dominate."),
        ts("End-to-end latency (p50 / p95)", 12, 6, 12, 8,
           [tgt("histogram_quantile(0.5, sum by (le) (rate(nexrad_ingest_end_to_end_seconds_bucket[30m])))", "p50"),
            tgt("histogram_quantile(0.95, sum by (le) (rate(nexrad_ingest_end_to_end_seconds_bucket[30m])))", "p95")],
           unit="s", desc="Radar volume start -> object durable in MinIO. Includes "
                          "the wait for the volume to finish scanning."),

        row("Throughput & size", 14),
        ts("Download throughput", 0, 15, 8, 7,
           [tgt("nexrad_ingest_download_mbps", "{{site}}")], unit="MBs", dec=1, minv=0,
           desc="Measured knee is 16 threads ~10-12 MB/s. Serial was 1.1 MB/s."),
        ts("Bytes in / out", 8, 15, 8, 7,
           [tgt("sum(rate(nexrad_ingest_bytes_in[10m]))", "source (Level II)"),
            tgt("sum(rate(nexrad_ingest_bytes_out[10m]))", "output (Zarr)")],
           unit="Bps", desc="Output currently ~1.66x source: Level II is heavily "
                            "bzip2'd, Zarr defaults to a faster codec."),
        ts("Size ratio (out / in)", 16, 15, 8, 7,
           [tgt("nexrad_ingest_size_ratio", "{{site}}")], dec=2, minv=0),

        row("Structure & errors", 22),
        ts("Zarr members vs objects written", 0, 23, 12, 7,
           [tgt("sum(rate(nexrad_ingest_zarr_members_per_volume_sum[1h])) / "
                "clamp_min(sum(rate(nexrad_ingest_zarr_members_per_volume_count[1h])),0.0001)",
                "zarr keys inside archive"),
            tgt("sum(rate(nexrad_ingest_objects_per_volume_sum[1h])) / "
                "clamp_min(sum(rate(nexrad_ingest_objects_per_volume_count[1h])),0.0001)",
                "objects in MinIO")],
           dec=0, minv=0,
           desc="The gap between these two IS the object-count saving: ~397 Zarr "
                "keys collapsed into 1 stored object."),
        ts("Errors by stage", 12, 23, 12, 7,
           [tgt("rate(nexrad_ingest_errors_total[5m])*300", "{{stage}}")],
           dec=2, minv=0, desc="download / decode / zarr / upload / loop / parse."),
    ],
}

# ===================== SCIT CORE =====================
scit = {
    "uid": "nexrad-scit", "title": "NEXRAD — SCIT Core", "tags": ["nexrad", "pipeline"],
    "timezone": "browser", "schemaVersion": 39, "refresh": "30s",
    "time": {"from": "now-6h", "to": "now"},
    "description": "Storm cell identification and tracking (detection_v2, "
                   "envelope-based). Admission gates reject shallow returns "
                   "BEFORE a cell exists — the fix for the old system emitting "
                   "tornado probabilities on sub-kilometre cells.",
    "panels": [
        row("Overview", 0),
        stat("Volumes analysed (1h)", 0, 1, 4, 4,
             [tgt("sum(increase(scit_volumes_total{status=\"ok\"}[1h]))", "ok")]),
        stat("Failures (1h)", 4, 1, 4, 4,
             [tgt("sum(increase(scit_volumes_total{status=\"error\"}[1h]))", "err")],
             thresholds={"mode": "absolute", "steps": [
                 {"color": "green", "value": None}, {"color": "red", "value": 1}]}),
        stat("Cells admitted (1h)", 8, 1, 4, 4,
             [tgt("sum(increase(scit_cells_total[1h]))", "cells")],
             desc="Zero in clear air is CORRECT — the gates are doing their job. "
                  "Cross-check against composite max dBZ below."),
        stat("Active tracks", 12, 1, 4, 4,
             [tgt("scit_active_tracks", "{{site}}")]),
        stat("Mean time / volume", 16, 1, 4, 4,
             [tgt("sum(rate(scit_total_seconds_sum[1h])) / "
                  "clamp_min(sum(rate(scit_total_seconds_count[1h])),0.0001)", "s")],
             unit="s", dec=2,
             desc="Baseline ~2.6s. The pyart gridding route measured 47s."),
        stat("Pending", 20, 1, 4, 4,
             [tgt("scit_pending", "pending"), tgt("scit_inflight", "in-flight")]),

        row("The admission gates", 5),
        ts("Composite max dBZ vs cells admitted", 0, 6, 12, 8,
           [tgt("histogram_quantile(0.95, sum by (le,site) (rate(scit_volume_max_dbz_bucket[30m])))",
                "p95 composite dBZ {{site}}"),
            tgt("sum by (site) (rate(scit_cells_total[30m]))*1800", "cells/30m {{site}}")],
           dec=1, minv=0,
           desc="READ THESE TOGETHER. Strong echo with zero cells is the gates "
                "rejecting shallow returns — e.g. a measured 56 dBZ echo confined "
                "to ONE 500 m level with a 2.5 km top was correctly refused. "
                "Cells appearing with low composite dBZ would be the alarming case."),
        ts("Echo top of admitted cells", 12, 6, 6, 8,
           [tgt("histogram_quantile(0.5, sum by (le) (rate(scit_cell_echo_top_km_bucket[1h])))", "p50"),
            tgt("histogram_quantile(0.05, sum by (le) (rate(scit_cell_echo_top_km_bucket[1h])))", "p05")],
           unit="lengthkm", dec=1,
           desc="p05 must never approach the echo_top_min_km gate (3 km). If it "
                "does, the gate has been relaxed — that is how the old system broke."),
        ts("Footprint area of admitted cells", 18, 6, 6, 8,
           [tgt("histogram_quantile(0.5, sum by (le) (rate(scit_cell_area_km2_bucket[1h])))", "p50")],
           dec=0, desc="min_area_km2 gate is 4."),

        row("Performance", 14),
        ts("Stage timings (mean)", 0, 15, 12, 7,
           [tgt("sum(rate(scit_fetch_seconds_sum[10m])) / clamp_min(sum(rate(scit_fetch_seconds_count[10m])),0.0001)", "fetch+open"),
            tgt("sum(rate(scit_grid_seconds_sum[10m])) / clamp_min(sum(rate(scit_grid_seconds_count[10m])),0.0001)", "grid"),
            tgt("sum(rate(scit_identify_seconds_sum[10m])) / clamp_min(sum(rate(scit_identify_seconds_count[10m])),0.0001)", "identify"),
            tgt("sum(rate(scit_persist_seconds_sum[10m])) / clamp_min(sum(rate(scit_persist_seconds_count[10m])),0.0001)", "persist")],
           unit="s", dec=3, stack=True, minv=0,
           desc="Baseline: fetch 1.9s, grid 0.6s, identify 0.03-0.3s, persist ~0."),
        ts("Errors by stage", 12, 15, 12, 7,
           [tgt("rate(scit_errors_total[5m])*300", "{{stage}}")], dec=2, minv=0),

        row("Provenance", 22),
        stat("Detection settings hash", 0, 23, 8, 4,
             [tgt("scit_settings_hash_info", "{{settings_hash}}")],
             color="fixed",
             desc="Every persisted cell carries this. A change here means the "
                  "knob set changed — detections before and after are not "
                  "comparable."),
        ts("Gates binned per volume", 8, 23, 16, 4,
           [tgt("sum(rate(scit_gates_binned_sum[10m])) / clamp_min(sum(rate(scit_gates_binned_count[10m])),0.0001)", "gates")],
           dec=0, desc="~5.2M radar gates per volume. A sharp drop means sweeps "
                       "are missing from the Zarr."),
    ],
}

print("publishing:")
publish(poller)
publish(ingest)
publish(scit)
