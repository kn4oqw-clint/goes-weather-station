# SCIT Revival — assessment & plan

Investigation 2026-07-26. Scope: what it takes to bring NEXRAD storm-cell
identification & tracking back, move to the **envelope-based `detection_v2`**,
run a **dual radar site** feed, and surface both **notifications and live radar**
in Home Assistant.

No code changes made — this is the plan.

---

## 1. Why it is down

Not a software fault. **The host disappeared.**

```
storm_cell_tracks newest : 2026-06-24 12:20   (32+ days stale)
alert_log         newest : 2026-06-24
172.16.50.240 (swarm / R820) : 100% packet loss
Proxmox VM list              : no NEXRAD/swarm VM present
```

Everything downstream is healthy and still waiting: celery beat fires
`threat-snapshot-refresh` every 60 s, and `run_matrix` is triggered by the MQTT
`volume_complete` sweep — which has had nothing to sweep since the processor
host went away. Tornado/severe/hail/wind alerting for all 31 registered devices
has been silently dead the whole time.

**Consequence:** rebuilding the processor host restores the *existing* alerting
with no application changes. That is the fastest win available and should be
phase 1.

---

## 2. What already exists and is reusable

| Repo | Role | Reuse |
|---|---|---|
| `WxAlerts-Nexrad-SQS-Consumer` | SNS→SQS E-chunk notifications → Celery dispatch. Complete, 84 tests, Prometheus, health endpoints | **Replace** (see §3) but lift its dedup cache + metrics protocol |
| `WxAlerts-Nexrad-Proc_v4` | Celery workers: fetch chunks → pyart decode → **polar Zarr v3** → MQTT `volume_complete/{site}/{epoch}/zarr` → SeaweedFS upload | **Keep nearly as-is** — this is the heart |
| `storm_modeler/detection_v2` | The new envelope SCIT: `identify.py`, `track.py`, `types.py`. Pure, no globals, explicit `DetectionParams` | **Promote to production** |
| `storm_modeler/detection/{vault,couplets,srm,cloudtop}` | Vault/overshooting-top (HRRR 0 °C), velocity couplets, storm-relative motion, GOES cloud-top | Optional enrichment, already written |
| `WxA-TiTiler`, `WxAlerts-FastAPI-Tiler` | Raster tiles for the Leaflet map | Reuse for the HA radar view |
| `api.wxalerts.org/matrix` | Clustering, closest-approach ETA, PostGIS prefilter, `alert_state` dedup, dispatch | Unchanged |

---

## 3. Ingest redesign — drop SQS, poll for E-chunks

Production used SNS→SQS for lowest latency. That latency isn't needed here, and
SQS is the piece with an AWS account dependency. Replace it with a plain
**anonymous bucket poll every 60–120 s**.

### Verified bucket layout

```
s3://unidata-nexrad-level2-chunks/<SITE>/<volseq>/<YYYYMMDD>-<HHMMSS>-<NNN>-<S|I|E>

KMOB/529/20260727-0218xx-001-S      2.4 KB   volume start
KMOB/529/20260727-0218xx-0NN-I    ~150 KB   intermediate
KMOB/529/20260727-0218xx-0NN-E             volume COMPLETE  <-- the trigger
```

Probed live 2026-07-27: **both sites are streaming right now** — `KMOB/529`
(32 chunks) and `KEVX/673` (61 chunks), newest objects seconds old, `E` not yet
present because the volumes were mid-scan. Anonymous (`UNSIGNED`) reads work; no
credentials required.

### Two gotchas found while probing

1. **Do not list every volume prefix.** KMOB currently has **417** volume dirs
   and KEVX **491** (~1.5 days retained). Listing each to find the newest is
   400+ API calls per poll. Instead: one `list_objects_v2(Prefix="SITE/",
   Delimiter="/")` to get the dirs, take the numerically-highest 2–3, and list
   only those — ~4 calls per site per poll.

2. **The volume sequence wraps.** It is a 3-digit counter (currently 529 / 673).
   Lexical sorting happens to work today but breaks at the 999→001 rollover,
   where `001` sorts before `998`. **Sort numerically and handle rollover**, or
   track the last-seen sequence per site and step forward. This is the kind of
   bug that lies dormant for months and then fires once.

### Poller contract

* per-site last-seen `(volseq, chunk)` cursor, persisted
* dispatch only when the `-E` object appears for a volume
* dedup on `{site}-{volseq}-E` (lift `DedupCache` from the SQS consumer)
* emit the same Celery task the proc workers already consume, so
  `nexrad-proc-v4` needs no change

---

## 4. SCIT: replace per-gate tracking with `detection_v2`

The production SCIT tracked essentially every high-dBZ gate — hence 2.79 M rows
in `storm_cell_tracks` and the `matrix/worker.py` note that *"tracks ARE
multi-typed (count 2407 in prod); clustering is purely spatial"*. The matrix
worker is doing greedy spatial re-clustering downstream precisely to undo that.

`detection_v2` produces **storm envelopes directly**, so that downstream
re-clustering becomes redundant:

1. **Seed** on `seed_dbz` → 3D connected components
2. **Watershed split** at reflectivity saddles, so a merged multi-core system
   resolves into disjoint cells instead of one blob
3. **Grow to the base-reflectivity footprint** — the envelope — partitioned
   among seeds by a 2D watershed on column-max reflectivity, so multi-core
   systems don't each claim the whole system
4. **Admission gates that reject anomalous propagation**: `continuity_levels`
   vertical levels, `echo_top_min_km`, `min_area_km2`. Ground clutter confined
   to the lowest tilt fails both — this is the documented Phase-1 guarantee
5. **Envelope** emitted as a convex-hull polygon in lon/lat
6. **Tracker**: greedy nearest-neighbour gated by absolute displacement
   (`track_max_km`), surviving `track_miss_max` unmatched volumes

`StormCell` already carries everything the alert matrix wants: `envelope`,
`seed_lon/lat`, `max_dbz`, `area_km2`, `echo_top_km`, `depth_km`, `track_id`,
plus optional `cloud_top_c`, `freezing_level_km`, `vault_depth_km`,
`overshooting_top`.

**Migration note:** `cells_v2`/`warnings_v2` in storm_modeler carry a
`settings_hash` tracing every row to the exact tunable set. Keep that in
production — it is what makes a threshold change auditable rather than a mystery.

---

## 5. Dual site — KMOB + KEVX is close to ideal

```
KEVX  108.8 km  bearing  94.3deg (E)   Eglin AFB FL
KMOB  113.4 km  bearing 272.3deg (W)   Mobile AL
```

The station sits almost exactly **midway between them, on near-opposite
bearings** (178° apart) at nearly equal range. That gives:

* redundancy — one site's cone of silence is the other's mid-range
* ~110 km is a good working range: past the cone, before severe beam broadening
* near-orthogonal-to-antiparallel look angles, which is the useful geometry for
  cross-checking cell position and, later, dual-Doppler-style velocity work

Next nearest is KEOX at 177 km, materially worse. **KMOB + KEVX is the pair.**

Tracking must be **per-site** (`Tracker` instances keyed by site) with
cross-site association handled above it — the same physical storm seen by both
radars must not become two tracks in an alert. `StormCell.site` already exists;
the merge step does not.

---

## 6. Home Assistant integration

Two separate things:

**Notifications** — the station already has a working, FCM-free pattern from the
GLM lightning work ([lightning-glm.md](lightning-glm.md)): a service publishes
MQTT with HA discovery, and HA automations own delivery. Do the same for storm
cells: nearest-cell distance, ETA, max dBZ, echo top, hazard flags, and a
`device_class: safety` binary per threshold. **Do not route home alerts through
FCM** — that is the wxalerts.org product path.

**Live NEXRAD radar** — the tilers already exist. HA can show it via a
`camera` fed by rendered tiles, or a Lovelace `picture-elements`/custom card
pointed at the Leaflet tile endpoint. The Zarr → tile path in `nexrad-proc-v4` +
TiTiler is the piece that makes this nearly free once the pipeline is back.

---

## 7. Hosting — the real constraint

Current Proxmox (`pve`, 172.16.50.10):

```
CPU      : 24 threads, Xeon X5670 @ 2.93 GHz (2010-era)
RAM      : 133 GiB total, 111 GiB used, ~22 GiB available
Local_Array (ZFS) : 13.9 TB total, 3.3 TB free (76 % used)
local-lvm         : 70 GB free (79 % used)
load average      : ~2.5 / 24 threads  (CPU is not the bottleneck)
```

**RAM is the binding constraint.** A dual-site processor wants ~12–16 GB
(pyart gridding is 1–2 GB per concurrent volume, plus Redis, SCIT, tiler).
That fits in 22 GiB, but leaves the host with almost nothing spare.

**Storage matters more than it looks.** Polar Zarr is roughly 50–100 MB per
volume. Two sites at ~5–6 min volumes ≈ 550 volumes/day ≈ **40+ GB/day**.
`local-lvm` (70 GB free) cannot hold that — put the VM disk / scratch on
`Local_Array`, keep local retention short (hours), and let SeaweedFS be the
archive, exactly as v4 already does.

Recommended VM: **8 vCPU / 14 GB RAM / 200 GB on Local_Array**, Debian 13,
Docker. Consider reclaiming RAM elsewhere first — 22 GiB free on a 133 GiB host
is tight enough that one more service could destabilise it.

---

## 8. Suggested phasing

| Phase | Work | Outcome |
|---|---|---|
| 1 | Provision the VM; deploy `nexrad-proc-v4` + Redis + the new E-chunk poller, **single site (KMOB)** | Existing tornado/severe/hail/wind alerting **comes back to life** with zero app changes |
| 2 | Add KEVX; per-site trackers + cross-site cell association | Dual coverage, no duplicate alerts |
| 3 | Swap production SCIT for `detection_v2`; keep `settings_hash`; run **shadow** beside the old rows and compare | Envelope tracking, far fewer/cleaner cells |
| 4 | MQTT storm-cell publisher + HA automations (FCM-free) | Storm alerts in HA |
| 5 | Zarr → TiTiler tiles → HA Lovelace | **Live NEXRAD radar in Home Assistant** |
| 6 | Optional: vault/overshooting-top, velocity couplets, SRM, GOES cloud-top | Richer hazard evidence |

Phase 1 is the high-value one: it is mostly provisioning, and it ends a 32-day
silent outage.

⚠️ **`ALERT_LIVE=true` on the celery worker.** The moment radar data flows
again, real pushes resume to 31 devices. Decide deliberately whether to bring
phase 1 up with `ALERT_LIVE=false` first and watch `alert_log` in shadow.
