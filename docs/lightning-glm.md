# GLM Lightning Proximity → Home Assistant

Per-target lightning proximity (nearest strike, ring counts, rate, trend) for
the house and for every person Home Assistant is tracking. Home Assistant owns
notification delivery; the service only produces state.

Built 2026-07-26. This is one half of a two-path design — see
[Relationship to wxalerts.org](#relationship-to-wxalertsorg).

---

## Where the data comes from — read this first

**GLM is not received by this station's dish.** The Geostationary Lightning
Mapper rides on **GRB**, not HRIT. The RTL-SDR / `goesrecv` / `goesproc` chain
physically cannot decode it, and `/srv/goes` on the receiver LXC contains zero
GLM products (verified 2026-07-26).

The data arrives over the **internet** instead. The `wxalerts-glm-ingest`
container on `172.16.50.211` anonymously polls NOAA's public AWS bucket:

```
s3://noaa-goes19/GLM-L2-LCFA/YYYY/DDD/HH/OR_GLM-L2-LCFA_G19_sYYYYDDDHHMMSSf_*.nc
GLM_BUCKET=noaa-goes19   GLM_PRODUCT=GLM-L2-LCFA   GLM_POLL_INTERVAL_S=60
boto3 client with signature_version=UNSIGNED   (no credentials needed)
```

and bulk-inserts into `glm_flashes`. Measured performance:

| Metric | Value |
|---|---|
| Flash-time → queryable latency | **~19 s** |
| Volume | ~550 k flashes / 24 h |
| Proximity query (GiST KNN) | ~0.27 s |

### The consequence that shapes the design

This feed is **internet-dependent**. If the uplink dies the data stops — and
that is precisely when a storm is overhead. Worse, during a wifi/ethernet
outage the station falls back to a **50 MB/month cellular cap**, where polling
S3 is the last thing you want.

So the service treats staleness as `unavailable`, never as "0 strikes".
A confident zero during an outage is the dangerous failure mode. `GLM_STALE_SEC`
(default 600 s) governs this.

**What would make it outage-proof:** a local AS3935 (poor data, survives
outages) or a real GRB downlink with the bigger dish (good data, no internet
dependency). The GRB upgrade buys independence, not just resolution.

---

## Schema

```sql
glm_flashes                       -- Timescale hypertable, 32 partitions
  flash_time  timestamptz NOT NULL   -- btree DESC
  satellite   text        NOT NULL
  geom        geography(Point,4326)  -- GiST
  energy_j    double precision
  area_m2     double precision
```

`geography` (not `geometry`) means `ST_Distance` / `ST_DWithin` return true
metres on the spheroid — no projection needed.

---

## Service

`glm_lightning.py` on the wxstation Pi (`10.10.0.171`), unit
`glm-lightning.service`. Secrets in `~/.glm_lightning.env` (mode `0600`, never
committed) — DB DSN, HA token, MQTT password, station coordinates, thresholds.

### Targets

1. **Home** — the fixed station position (`STATION_LAT` / `STATION_LON`).
2. **Every HA `person.*` entity with a current GPS fix**, re-discovered on each
   poll. Adding a person in Home Assistant is all that is needed; there is no
   target list to edit here. Persons without a GPS fix (e.g. `person.wxnode`)
   are skipped automatically.

### Published entities

One HA device per target, named `Lightning — <name>`:

| Entity | Meaning |
|---|---|
| `Nearest Strike` | miles to closest flash in the window |
| `Strikes 10mi` / `Strikes 30mi` | counts inside the ring |
| `Strike Rate` | flashes/min within 30 mi |
| `Since Last Strike` | minutes |
| `Trend` | Approaching / Receding / Steady / Unknown |
| `Lightning Within 10mi` | **binary_sensor, device_class `safety`** — automations key off this |

### Tunables (`~/.glm_lightning.env`)

| Var | Default | Notes |
|---|---|---|
| `GLM_ALERT_MI` | `10` | NWS seek-shelter distance; drives the binary sensor |
| `GLM_WINDOW_MIN` | `30` | look-back for counts and rate |
| `GLM_POLL_SEC` | `45` | poll cadence |
| `GLM_STALE_SEC` | `600` | ingest age beyond which targets go `unavailable` |

**Why 10 miles:** matches NWS *When Thunder Roars* guidance and the 30-30 rule.
The wxalerts `fcm_devices.alert_radius_km` default of 30 km (18.6 mi) is a
*storm* radius; for lightning specifically, 10 mi is the distance at which you
are actually at risk.

### Trend logic

Compares first-vs-last nearest-distance over a 5-sample history rather than
consecutive samples, so one noisy flash on the far edge of a cell doesn't flip
the label. Needs ≥3 samples (~2¼ min) before it reports anything but `Unknown`.

---

## Validation performed

The service's real query path was run against a point with known active
lightning, to prove that zeros at home are genuine and not a silent failure:

```
ACTIVE STORM (S Pacific)   nearest=0.0 mi  10mi=1099  30mi=1099  rate=36.63/min  nearby=True
Home (station)             nearest=None    10mi=0     30mi=0     rate=0.0/min    nearby=False
```

Re-run this check after any query change — a proximity sensor that always
returns zero looks identical to a quiet sky.

---

## Relationship to wxalerts.org

The wxalerts.org platform has its own independent alerting stack:

```
storm_cell_tracks ──▶ matrix/worker.py ──▶ hazards/evaluators.py ──▶ matrix/fcm.py
   (NEXRAD SCIT)      cluster + ETA +        tornado/severe/hail/         FCM push
                      PostGIS prefilter      wind/lightning              to fcm_devices
                      + alert_state dedup
```

**This service is deliberately decoupled from that.** No FCM, no `fcm_devices`.
Home targets come from HA `person.*`; delivery is HA automations. The two paths
share only the `glm_flashes` table.

Two things to know about the platform side:

1. **`hazards/evaluators.py::lightning()` is still a stub** — it returns `None`
   with the comment *"Replace when the GLM ingestion pipeline lands"*. The
   pipeline has landed; the evaluator was never updated.
2. **`ALERT_LIVE=true` on the celery worker.** Anything wired into that path
   pushes to real phones immediately (31 devices, all with `prefs.lightning`
   true). Roll out in shadow first.

### ⚠️ The platform's storm path is currently dead

```
storm_cell_tracks newest : 2026-06-24 12:20:09   (32 days stale)
alert_log         newest : 2026-06-24
```

NEXRAD ingestion stopped on 2026-06-24, so `run_matrix` — triggered by the MQTT
`volume_complete` sweep — has had nothing to sweep. Tornado / severe / hail /
wind alerting has been silently down since then. Celery beat is still healthy
(`threat-snapshot-refresh` fires every 60 s), so the outage is upstream in radar
ingest, not in the scheduler.

This is also *why* the GLM service is device-centric rather than storm-centric:
hanging lightning off `StormEvidence` would inherit the same dead input.

---

## Home Assistant automations — INSTALLED 2026-07-27

Five automations are live in HA. The file
[`home-assistant/lightning-automations.yaml`](../home-assistant/lightning-automations.yaml)
is **exported from the running instance**, so it cannot drift from what is
actually installed; round-trip tooling is in
[`home-assistant/tools/`](../home-assistant/tools/README.md).

| automation | fires | notifies |
|---|---|---|
| `lightning_near_clint` | Clint's `within_10mi` off→on | Clint's Pixel 10 Pro |
| `lightning_near_sierra` | Sierra's `within_10mi` off→on | Sierra's iPhone |
| `lightning_near_home` | station `within_10mi` off→on | both phones + persistent notification |
| `lightning_all_clear_home` | station off for 30 min | both phones, dismisses the notification |
| `lightning_feed_stale` | station `unavailable` for 10 min | Clint + persistent notification |

**Policy:** each person is alerted on their own phone for lightning near *them*;
a strike near the house notifies the whole household.

**Device map — verified against `person.device_trackers`, not assumed:**

```
person.clint_chance  -> device_tracker.pixel_10_pro    -> notify.mobile_app_pixel_10_pro
person.sierra_chance -> device_tracker.sierra (iPhone) -> notify.mobile_app_iphone
```

`device_tracker.clint_s_phone` and `device_tracker.sm_s918u` also exist but are
linked to no person, so they are deliberately not targeted — and `notify.notify`
is avoided for the same reason, since it would broadcast to them too.

**Why the legacy `notify.mobile_app_*` service rather than
`notify.send_message`:** only it carries the delivery hints that decide whether
a seek-shelter alert actually wakes someone — Android `importance: high` on a
dedicated channel, iOS `interruption-level: time-sensitive`. `send_message`
takes title and message only.

**Why every alert trigger has `from: "off"`:** the service publishes
`unavailable` when the feed goes stale, so an `unavailable → on` transition is
the feed *recovering*, not news about the sky. Without the guard every ingest
hiccup would fire a false lightning alert.

## Weather Station dashboard

The `weather-station` dashboard's **Now** view has a `Lightning (GOES-19 GLM)`
section, inserted directly below NWS Alerts and above the GOES loop — lightning
proximity is the one item on the page that might require acting within minutes.

It contains a per-target glance row (within-10mi flag, nearest strike, count,
trend) for Home, Clint and Sierra, plus two conditional cards that are hidden on
a calm day:

* a prominent **⚡ LIGHTNING WITHIN 10 MILES** panel listing which targets are
  affected, shown only when at least one is;
* an **⚠️ feed unavailable** panel shown when the station sensor goes
  `unavailable` — because a confident, quiet-looking zero during an internet
  outage is the dangerous failure, and absence of data has to look different
  from absence of lightning.

---

## Ideas / next steps

- Implement `hazards/evaluators.lightning()` using GLM flash rates within the
  storm cluster radius, for when the radar path is restored.
- Strike-density map overlay for the HA dashboard.
- Feed `device_telemetry.lightning_count` / `lightning_distance_mi` (columns
  already exist, originally intended for the AS3935 that never worked).
- Suppress polling while on cellular failover so it can't eat the 50 MB cap.
