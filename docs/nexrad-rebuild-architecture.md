# NEXRAD / SCIT Rebuild — architecture & capacity plan

Full replacement of the wxalerts NEXRAD pipeline. The previous stack ran on an
R820 monitoring many radar sites; this targets **two sites (KMOB + KEVX)** on the
existing **R710** Proxmox host.

Planning doc, 2026-07-26. Supersedes the phased approach in
[scit-revival-plan.md](scit-revival-plan.md), which assumed the old architecture
could be revived — it cannot.

---

## 0. Why the old system was taken offline

> The original detection system was issuing **tornado probabilities for cells
> that were not even a kilometre in height.**

That is a *detection* failure being surfaced as a *scoring* output, and it is the
single most important thing the new architecture must structurally prevent.

`detection_v2` fixes it at the right layer — cells must clear admission gates
**before they exist as cells at all**:

| Gate | Rejects |
|---|---|
| `continuity_levels` | echoes present on too few vertical levels |
| `echo_top_min_km` | shallow returns — the sub-kilometre case |
| `min_area_km2` | speckle |

Ground clutter and anomalous propagation are confined to the lowest tilt and fail
both structural gates. **The design principle: gate at identification, never at
scoring.** A cylinder like TorNET must never be handed a cell that shouldn't
exist — filtering downstream is how the old system produced its bad output.

---

## 1. Host audit — what's actually consuming the R710

```
PowerEdge R710 · 2× Xeon X5670 (24 threads) · 133 GiB RAM
RAM: 111 used / 21 available
```

### RAM — the bottleneck is not the VMs

| Consumer | Allocated | Actual | Note |
|---|---|---|---|
| **ZFS ARC** | **132.7 GiB c_max** | **66.5 GiB** | ⚠️ **no cap set** |
| wazuh (150) | 12 GiB | 11.3 GiB | consolidation candidate |
| debian12-docker (101) | 9 GiB | 8.5 GiB | |
| grafana (105) | 8 GiB | 7.6 GiB | |
| hermes (210) | 8 GiB | 7.5 GiB | |
| haos16.2 (113) | 4 GiB | 3.9 GiB | |
| **arr-media (111)** | **12 GiB** | **0.7 GiB** | ⚠️ 11 GiB idle |
| all 10 LXCs | ~20 GiB | small | |

**ZFS ARC at 66.5 GiB with `c_max` unbounded is the whole problem.** There is no
`/etc/modprobe.d/zfs.conf`, so ARC defaults to consuming essentially everything
free. ARC is reclaimable in theory, but reclaim under sudden VM pressure is slow
and unreliable — capping it explicitly is standard practice on Proxmox.

### ✅ Remediation completed 2026-07-26

| Action | Result |
|---|---|
| **`wazuh` (VM 150) destroyed** | −12 GiB RAM, **−96 GB on `local-lvm`** (79 % → **50 %**, 166 GB free). Config saved to `wazuh-vm150.conf` if it is ever rebuilt |
| **ARC capped at 16 GiB** | `/etc/modprobe.d/zfs.conf` + applied live; ARC fell 66.5 → 15.9 GiB immediately |

```
Available RAM:  21 GiB  ->  81 GiB
Free RAM:                   70 GiB
local-lvm:      79 % used  ->  50 % used (166 GB free)
```

`arr-media` is still 12 GiB allocated for 0.7 GiB used — another ~8 GiB is
available if ever needed, but it is no longer required.

### Storage — everything is on the small pool

| Storage | Size | Free | Contents |
|---|---|---|---|
| `local-lvm` (thin) | 320 GB | **70 GB (79 % used)** | **every VM and CT** |
| `Local_Array` (ZFS) | 13.9 TB | 3.3 TB | **nothing** |
| `local` (dir) | 98 GB | 67 GB | ISOs/templates |

Nothing lives on the 13.9 TB array. `wazuh` alone provisions a **150 GB** disk —
roughly half the thin pool.

**Decision (owner): everything stays on the SSD pool.** No guest OS disks move to
the spinning array. Removing wazuh alone reclaimed enough thin-pool space to make
migration unnecessary.

Only *bulk data* goes to `Local_Array` — via MinIO (§3.6), never as guest root
disks.

---

## 2. Capacity for two sites

The R820 was sized for many sites. Two is roughly an order of magnitude less work:

* 2 sites × ~1 volume / 5–6 min ≈ **24 volumes/hour**
* decode + grid ≈ 20–60 s CPU per volume on an X5670
* ⇒ ~20 min of CPU per hour against **24 threads**

**CPU is comfortable.** Host load average is ~2.5/24 today. The X5670 is
2010-era and single-thread slow, but this workload is numpy-heavy and
parallelises across volumes.

**Storage is the pressure point:** polar Zarr ≈ 50–100 MB/volume × ~550
volumes/day ≈ **40+ GB/day**. That lands in **MinIO** (backed by `Local_Array`, 3.1 TB free) —
see §3.6, where object *count* rather than bytes is the real constraint.

---

## 3. Target architecture

```
                    ┌──────────────────────────────────────────┐
                    │  s3://unidata-nexrad-level2-chunks       │
                    │  <SITE>/<volseq>/<ts>-<NNN>-<S|I|E>      │
                    └───────────────┬──────────────────────────┘
                                    │ anonymous poll, 60–120 s
                                    ▼
   ┌────────────────┐   volume-complete   ┌─────────────────────────────┐
   │ nexrad-poller  │────── event ───────▶│ nexrad-ingest               │
   │ no SQS         │                     │  download chunks            │
   │ E-chunk cursor │                     │  pyart decode + grid        │
   │ per-site dedup │                     │  ── tmpfs RAM-disk buffer ──│
   └────────────────┘                     │  → polar Zarr               │
                                          └──────┬──────────┬───────────┘
                                                 │          │
                              Zarr (Local_Array) │          │ Zarr
                                                 ▼          ▼
                                        ┌──────────────┐  ┌────────────────┐
                                        │ nexrad-tiler │  │ scit-core      │
                                        │ → Leaflet/HA │  │ detection_v2   │
                                        └──────────────┘  │ identify+track │
                                                          └───────┬────────┘
                                                    cells + envelopes + track_id
                                                                  │
        ┌──────────┬──────────┬──────────┬──────────┬─────────────┼──────────┐
        ▼          ▼          ▼          ▼          ▼             ▼          ▼
     ┌─────┐   ┌───────┐  ┌──────┐  ┌─────────┐ ┌──────────┐ ┌──────┐  ┌──────────┐
     │ GLM │   │TorNET │  │ TGEN │  │CloudTop │ │Overshoot │ │ HRRR │  │NWS Alerts│
     └──┬──┘   └───┬───┘  └──┬───┘  └────┬────┘ └────┬─────┘ └──┬───┘  └────┬─────┘
        └──────────┴─────────┴───────────┴───────────┴──────────┴───────────┘
                                    │  per-cell attributes
                                    ▼
                          ┌────────────────────────┐
                          │      COMPARATOR        │
                          │ cell + full track      │
                          │ history → severity now │
                          │ and forecast trend     │
                          └───────────┬────────────┘
                                      │
                        ┌─────────────┴──────────────┐
                        ▼                            ▼
              ┌──────────────────┐        ┌────────────────────┐
              │ FCM (wxalerts)   │        │ MQTT → Home Asst.  │
              │ 31 devices       │        │ FCM-free, per §7   │
              └──────────────────┘        └────────────────────┘
```

### 3.1 `nexrad-poller` — replaces SQS

> ✅ **BUILT AND RUNNING 2026-07-26** — `/opt/nexrad` on `nexrad-proc`
> (10.10.0.68). See [Deployed stack](#deployed-stack) at the end of this doc.

Verified bucket layout (probed live 2026-07-27; both sites streaming):

```
KMOB/529/20260727-0218xx-001-S      2.4 KB   start
KMOB/529/20260727-0218xx-0NN-I    ~150 KB   intermediate
KMOB/529/20260727-0218xx-0NN-E             COMPLETE ← the trigger
```

Two gotchas found while probing, both of which will bite a naive implementation:

1. **Do not enumerate every volume prefix.** KMOB retains **417** volume dirs and
   KEVX **491** (~1.5 days). Listing each is 400+ API calls per poll. Instead:
   one delimited list to get the dirs, take the numerically-highest 2–3, list
   only those — ~4 calls per site per poll.
2. **The volume counter wraps.** It's a 3-digit sequence (currently 529 / 673).
   Lexical sort works *today* and breaks silently at 999→001, where `001` sorts
   before `998`. **Sort numerically and handle rollover**, or carry a per-site
   cursor. Classic dormant bug.

Lift the `DedupCache` and `MetricsProtocol` from `WxAlerts-Nexrad-SQS-Consumer` —
they're written and tested; only the transport changes.

### 3.2 `nexrad-ingest` — RAM-disk buffered

The chunk→grid→Zarr path is write-heavy and entirely transient. Stage it on
**tmpfs** so none of it touches disk:

* raw chunks: ~10–15 MB/volume
* pyart grid in flight: ~1–2 GB/volume (6 moments)
* Zarr staging: 50–100 MB/volume

With 81 GiB now available, the RAM tier can be generous: **8 GiB tmpfs for
in-flight work + a 24–32 GiB RAM cache of recent volumes** for SCIT and the
tiler to read without touching storage. Oldest volumes evict to MinIO.

⚠️ **tmpfs counts against RAM** and competes with ARC — which is exactly why the
ARC cap had to land first. Budget it explicitly (§5).

### 3.6 Storage — MinIO, and the small-file problem

**MinIO, not SeaweedFS.** The existing MinIO (CT 203, `10.10.0.10`) already
serves the GOES archive and is backed by `/Local_Array/minio_s3` with **3.1 TB
free**.

Worth being explicit about, because it collapses a tier: **MinIO's backing store
*is* the ZFS array.** "Cache in RAM, then move it off to ZFS" and "put hot Zarr in
MinIO" are therefore the *same* destination — there is no separate third tier to
build:

```
tmpfs (RAM)  ──evict──▶  MinIO  ──(already on)──▶  Local_Array ZFS
 in-flight +              hot Zarr,                 3.1 TB free
 recent volumes           served to tiler/SCIT
```

#### ⚠️ The R820 bottleneck: object count, not bytes

> *"We had a bottleneck when I tried this on the 820 just due to the sheer number
> of files I was moving."*

That is the defining constraint here, and naive Zarr makes it worse. A polar Zarr
store writes **one object per chunk per variable** — easily 500–2000 objects per
volume. At ~550 volumes/day that is **300 k–1 M objects/day**, and both MinIO and
ZFS degrade on object *count* long before they run out of *bytes*.

**Mitigations, in order of effect:**

1. **Zarr v3 sharding — the actual fix.** Sharding packs many chunks into a
   single shard object while keeping chunk-level random reads. It routinely cuts
   object count 10–100×, turning ~1000 objects/volume into ~10–50. v4 already
   writes Zarr **v3**, so this is a store-configuration change, not a rewrite.
   *Do this before anything else.*
2. **Chunk deliberately.** Size chunks to the read pattern (SCIT wants whole
   sweeps; the tiler wants spatial tiles). Fewer, larger chunks beat many small
   ones for both.
3. **Never PUT per chunk.** Write the finished store in tmpfs, then upload as a
   batch. Per-object round-trips over the network are what actually killed the
   R820 throughput.
4. **Consider a single-file store** (`ZipStore`) for cold volumes — one object
   per volume, trivially cheap to move, at the cost of random access.
5. **Retention on object count, not just age.** Track objects/day and prune on it.

#### Key layout — never use NOAA's volume counter

> *"We should never be using their 3-digit counter. Build the folders by the
> volume date/time or the EPOCH."*

Agreed, and it is the correct call — that counter is a **rotating 3-digit
sequence** that wraps at 999→001. It is fine for *finding* new volumes in NOAA's
bucket, and unusable as an identity in ours.

```
s3://wxalerts-nexrad/<SITE>/<YYYY>/<MM>/<DD>/<HH>/<SITE>_<epoch>.zarr
                                                   └── volume start, UTC epoch
```

Chronologically sortable, collision-free across the wrap, prefix-listable by
hour/day for retention sweeps, and it makes "the volume at time T" a direct key
rather than a lookup. Take the timestamp from the **chunk filename**
(`20260727-021801-...`), never from the directory number.

### 3.3 `scit-core` — its own container

`detection_v2` only: `identify()` + `Tracker`, nothing else. Pure, no globals,
explicit `DetectionParams`. Keep `settings_hash` on every persisted row so a
threshold change is auditable rather than a mystery.

**Per-site trackers**, with cross-site association above them — the same physical
storm seen by KMOB and KEVX must not become two tracks. `StormCell.site` exists;
the merge step does not and must be written.

### 3.4 Engine cylinders

Each is an independent worker consuming `(cell, envelope, valid_time)` and
returning attributes. They must be **individually failable** — a TorNET outage
degrades confidence, it does not stop the pipeline.

| Cylinder | Input | Adds | Status |
|---|---|---|---|
| GLM | `glm_flashes` PostGIS | flash rate/density in envelope | **data already live** ([lightning-glm.md](lightning-glm.md)) |
| TorNET | gridded volume | tornado probability | `wxalerts-tornet` repo exists |
| TGEN | gridded volume | tornadogenesis probability | `WxAlerts-tgen-proc` repo exists |
| Cloud Top | GOES ABI ch13 | cloud-top temp per cell | calibrated LUT solved — [goes-derived.md](goes-derived.md) |
| Overshoot | HRRR 0 °C + radar | vault depth, overshooting top | `detection/vault.py` written |
| HRRR | `noaa-hrrr-bdp-pds` | environmental context | idx byte-range pull already proven in storm_modeler |
| NWS Alerts | NWS API | containing warning polygons | `_encompassing_nws_id()` exists in matrix worker |

Also already written in storm_modeler and worth folding in: `couplets.py`
(velocity couplets / rotation) and `srm.py` (storm-relative motion).

### 3.5 Comparator

The piece that does not exist yet, and the one that matters most.

Consumes the cell **plus its full track history** and produces a severity score
now *and* a trend. Requirements:

* **history-aware** — a cell whose vault has deepened and whose GLM flash rate
  has tripled over three volumes is a different animal from the same instantaneous
  snapshot with no trend. Store per-track attribute time series in a Timescale
  hypertable keyed on `track_id`.
* **explainable** — carry the evidence chips (`HazardResult.chips` already models
  this) so an alert can state *why*.
* **degradation-aware** — score with explicit confidence when cylinders are
  missing, rather than silently treating absent as benign.
* **hysteresis** — severity must not flap volume-to-volume; require persistence
  before escalating, and decay rather than cliff-edge on de-escalation.

---

## 4. Container inventory

| Container | vCPU | RAM | Notes |
|---|---|---|---|
| `nexrad-poller` | 1 | 512 MB | trivial |
| `nexrad-ingest` | 8 | 4 GB + **8 GB tmpfs** + RAM cache | the heavy one |
| `scit-core` | 2 | 2 GB | |
| cylinders (7, mixed) | 4 total | 4 GB total | TorNET/TGEN may want more if GPU-less |
| `comparator` | 2 | 2 GB | |
| `nexrad-tiler` | 2 | 2 GB | |
| Redis / bus | 1 | 1 GB | |
| **Total** | **~20 vCPU** | **~24 GB** | one VM, Docker Compose |

Recommend **one VM: 12 vCPU / 28 GB RAM / 200 GB on `local-lvm` (SSD)**, running the
whole compose stack. Splitting across VMs costs RAM in guest kernels for no
benefit at this scale.

---

## 5. RAM budget — actual, post-remediation

| | GiB |
|---|---|
| Available before | 21 |
| + wazuh removed | +10 (measured) |
| + ARC capped 66.5 → 15.9 GiB | +50 (measured) |
| **Available now** | **81** |
| − NEXRAD VM (28, incl. 8 tmpfs + RAM cache) | −28 |
| **Headroom remaining** | **~53** |

Comfortable, with room to grow the RAM cache if the object-count mitigations in
§3.6 leave I/O headroom. `arr-media` still holds ~8 GiB it does not use, in
reserve.

## 6. Build order

1. ~~**Remediate the host**~~ — ✅ **done 2026-07-26**: wazuh destroyed, ARC
   capped. 21 → **81 GiB** available; `local-lvm` 79 % → 50 %.
2. **Provision the VM** — OS disk on `local-lvm` (SSD, per owner decision);
   bulk data to MinIO only.
3. **Poller + ingest, single site (KMOB)** — prove chunks → Zarr end to end.
4. **Tiler** — earliest visible win, and it gives **live NEXRAD in Home Assistant**.
5. **`scit-core`** with `detection_v2`, single site, persisting cells + tracks.
6. **Add KEVX** + cross-site association.
7. **Cylinders**, cheapest-first: GLM (data already live) → Cloud Top → NWS
   Alerts → HRRR/Overshoot → TGEN → TorNET.
8. **Comparator**, in shadow, scoring against history.
9. **Dispatch** — MQTT/HA first (no blast radius), FCM last.

⚠️ **`ALERT_LIVE=true` on the celery worker.** Whatever restores the alert path
resumes real pushes to 31 devices. Bring the comparator up in shadow and diff it
against `alert_log` before enabling dispatch — especially given the sub-kilometre
tornado-probability history.

---

## 7. Home Assistant — keep it FCM-free

The GLM lightning work established the pattern: a service publishes MQTT with HA
discovery; HA automations own delivery. Do the same for storm cells (nearest cell,
ETA, max dBZ, echo top, hazard flags, severity) and for the radar tiles.

**Do not route home alerts through FCM** — that is the wxalerts.org product path.


---

## Deployed stack

`/opt/nexrad/docker-compose.yml` on **nexrad-proc (10.10.0.68)**.

| Container | Role |
|---|---|
| `nexrad-redis` | Redis 7, **AOF `everysec`**, `maxmemory 2gb`, `noeviction` |
| `nexrad-poller` | S3 discovery → work units on the `nexrad:volumes` stream |

Both bound to `127.0.0.1` (Redis `:6379`, poller metrics `:9090`) — nothing is
exposed on the LAN.

### Why AOF and `noeviction`

A queued work unit is not reconstructible: NOAA's chunks age out of the bucket
within ~1.5 days, and once they're gone that scan is gone. Losing a queue entry
therefore loses a volume permanently, so the queue is persisted and Redis is
configured to **refuse writes rather than silently evict** under memory pressure.

### Work unit

```json
{
  "site": "KMOB",
  "volume_epoch": 1785125015,
  "volume_utc": "2026-07-27T04:03:35Z",
  "storage_key": "KMOB/2026/07/27/04/KMOB_1785125015.zarr",
  "bucket": "unidata-nexrad-level2-chunks",
  "source_prefix": "KMOB/535/",
  "chunk_keys": ["KMOB/535/20260727-040335-001-S", "..."],
  "chunk_count": 67,
  "bytes": 9343711,
  "discovered_at": "2026-07-27T04:23:51Z"
}
```

Note `source_prefix` carries NOAA's counter (`535`) **as provenance only**. Every
identity — `volume_epoch`, `storage_key`, and the dedup key — derives from the
volume start timestamp, so the 999→001 wrap cannot collide or mis-order.

### Measured behaviour

```
ENQUEUED KMOB 2026-07-27T04:03:35Z chunks=67 9.3MB -> KMOB/2026/07/27/04/KMOB_1785125015.zarr
ENQUEUED KEVX 2026-07-27T04:01:09Z chunks=67 9.5MB -> KEVX/2026/07/27/04/KEVX_1785124869.zarr

S3 calls : 6.0 per poll per site   (vs 400+ if enumerating every volume dir)
Dedup    : 6 suppressed per site over 4 polls — no re-enqueue
Volume   : ~9.4 MB / 67 chunks
Cadence  : ~8.7 min between volumes (clear-air VCP; precip mode is faster)
```

`MAX_AGE_MIN=30` stops a cold start from enqueuing ~1.5 days of backlog.

### Consuming the queue

Ingest should use a **consumer group** so delivery is at-least-once and stuck
work is visible in the pending-entries list:

```bash
XGROUP CREATE nexrad:volumes ingest $ MKSTREAM
XREADGROUP GROUP ingest worker-1 COUNT 1 BLOCK 5000 STREAMS nexrad:volumes '>'
XACK nexrad:volumes ingest <id>          # only after the Zarr lands in MinIO
```

**Do not `XACK` before the Zarr is durable in MinIO** — that is the whole point
of using a group rather than a list.

### Operations

```bash
cd /opt/nexrad
docker compose logs -f poller
docker compose ps
docker exec nexrad-redis redis-cli XLEN nexrad:volumes
curl -s localhost:9090/metrics | grep nexrad_poller
```

Metrics: `polls_total`, `enqueued_total`, `dedup_total`, `errors_total{stage}`,
`s3_calls_total`, `volume_lag_seconds`, `stream_depth`.

**`nexrad_poller_s3_calls_total / polls_total` is the canary** — if it climbs
above ~6, prefix selection has regressed toward enumerating everything.


---

## nexrad-ingest — built and measured 2026-07-26

Consumer group on `nexrad:volumes` → concurrent download → xradar decode →
Zarr v3 (sharded) in a ZipStore → MinIO → `XACK`. Staged entirely on the 8 GB
tmpfs; nothing transient touches disk.

### Load test — 8 volumes, both sites

| Stage | mean | note |
|---|---|---|
| download | **0.94 s** | 10.4 MB/s at 16 threads |
| decode | **5.17 s** | xradar, 13 sweeps mean |
| zarr write | **5.56 s** | the other half of the cost |
| upload | **0.32 s** | to MinIO on the same VLAN |
| **total** | **12.00 s** | per volume |

| Size / count | mean |
|---|---|
| source | 9.48 MB (67 chunks) |
| output | 15.78 MB |
| sweeps | 13.0 |
| **objects per volume** | **1.00** |
| *zarr members inside the archive* | *397.5* |

**Headroom:** at precip cadence (~5 min/site, 2 sites = 24 volumes/h) that is
24 × 12 s ≈ **4.8 min of work per hour — an ~8 % duty cycle on one worker.**
The R710 is not close to strained by two sites.

**Object count:** 576 objects/day instead of ~229,000 for a directory store.
That is the R820 bottleneck removed, not merely reduced.

**Storage:** ~9.1 GB/day, so MinIO's 3.1 TB free is roughly a year.

### Verified, not assumed

* concatenated realtime chunks form a valid `AR2V0006` volume — xradar reads
  14 sweeps with full dual-pol (`DBZH, VRADH, WRADH, ZDR, PHIDP, RHOHV, CCORH`)
* an object pulled back out of MinIO opens in **0.56 s** with `DBZH (360,1540)`,
  `units=dBZ`, range −33.0…31.0 — valid data, not just a present file
* clear-air gating live: **9 volumes skipped, 85 MB of downloads avoided**,
  both sites reporting VCP 35

### Gotchas hit while building

1. **Zip duplicate entries.** Writing straight into a `ZipStore` makes xarray
   emit each array's `zarr.json` twice (`Duplicate name: sweep_13/ZDR/zarr.json`)
   — a zip append cannot overwrite, and which duplicate a reader honours is
   reader-defined. Fixed by staging a directory store on tmpfs and zipping it;
   it is all RAM, so it costs nothing.
2. **Prometheus name collision.** `Counter("..._source_bytes_total")` registers
   base name `..._source_bytes` (the client strips and re-adds `_total`), which
   collided with the same-named Histogram. Hence `bytes_in` / `bytes_out`.
3. **Shard/chunk divisibility.** Sweeps differ in gate count (1832 vs 1192), so
   chunk sizes must be computed as true divisors per array — a constant raises
   `Chunk edge length ... not divisible`.

### Tuning left on the table

Output is **1.66× the source** (15.78 MB from 9.48 MB) because Level II is
heavily bzip2-compressed while Zarr defaults to a faster codec, and the zip is
`ZIP_STORED`. A stronger zstd level on the Zarr codec would trade CPU — of which
there is plenty spare at an 8 % duty cycle — for storage.

### Metrics (`:9091`)

Speed: `download_seconds`, `decode_seconds`, `zarr_write_seconds`,
`upload_seconds`, `total_seconds`, `end_to_end_seconds`, `download_mbps`.
Size: `source_bytes`, `output_bytes`, `bytes_in`, `bytes_out`, `size_ratio`.
Count: **`objects_per_volume`**, `zarr_members_per_volume`, `chunks_per_volume`,
`sweeps_per_volume`, `volumes_total{site,status}`, `errors_total{stage}`,
`inflight`, `pending`, `vcp`.

Poller (`:9090`) adds `skipped_total{reason}`, `current_vcp`,
`bytes_skipped_total`, `s3_calls_total`, `volume_lag_seconds`, `stream_depth`.

Both are plain Prometheus text endpoints on `127.0.0.1`, ready to scrape for a
Grafana dashboard.


---

## Observability — Grafana

**Folder `NEXRAD Pipeline`** (uid `nexrad-pipeline`) on Grafana v13.0.2 @
`172.16.50.146:3000`. One dashboard per pipeline stage, so each engine cylinder
gets its own page rather than everything collapsing into one wall of panels.

| Dashboard | uid | Covers |
|---|---|---|
| **NEXRAD — Poller** | `nexrad-poller` | discovery, VCP gating, call budget |
| **NEXRAD — Ingest** | `nexrad-ingest` | stage timings, object count, size |
| *(per cylinder)* | `nexrad-<cylinder>` | GLM, TorNET, TGEN, CloudTop, Overshoot, HRRR, NWS Alerts |

Source of truth is [`monitoring/grafana/mk_nexrad_dashboards.py`](../monitoring/grafana/mk_nexrad_dashboards.py).
It is idempotent (`overwrite: true`), so re-running redeploys in place. Adding a
cylinder means copying a dashboard dict and giving it a new `uid`.

```bash
GRAFANA_TOKEN=glsa_... python3 monitoring/grafana/mk_nexrad_dashboards.py
```

The token is read from the environment and deliberately **not** committed.

### Prometheus

Two scrape jobs added to `/home/cchance/grafana/prometheus/prometheus.yml`
(30 s interval), validated with `promtool` and hot-reloaded via
`curl -X POST localhost:9090/-/reload` — no restart:

```yaml
  - job_name: nexrad-poller     targets: ["10.10.0.68:9090"]
  - job_name: nexrad-ingest     targets: ["10.10.0.68:9091"]
```

Metrics ports were rebound from `127.0.0.1` to `10.10.0.68` so Prometheus can
reach them. **Redis stays on loopback** — it is the work queue, not telemetry.

### The two canaries

Each dashboard leads with the metric that matters most, not the prettiest one:

* **Poller — `s3_calls_total / polls_total`.** Design target ~6. Climbing means
  prefix selection has regressed toward enumerating all 400+ volume dirs.
  *Currently 6.000 for both sites.*
* **Ingest — `objects_per_volume`.** Must stay at **1**. This is the metric that
  sank the R820. Panelled beside `zarr_members_per_volume` (~429) so the gap
  between them — the saving — is visible at a glance.
  *Currently 1.000 vs 429.*

### ⚠️ Stale scrape targets

Five jobs still point at `100.73.32.52` (the decommissioned R820 / swarm host)
and have been **down for a month**: `node-exporter`, `cadvisor`,
`postgres-exporter`, `redis-exporter`, and the old `nexrad-proc` job. The new
jobs supersede the last of those. They were left in place rather than removed —
worth a deliberate cleanup pass.


---

## scit-core — built and validated 2026-07-27

Consumer group on `nexrad:zarr` → fetch Zarr from MinIO → grid polar→Cartesian →
`identify()` → `Tracker.update()` → persist to PostGIS → announce on
`nexrad:cells`. Metrics on `:9092`, dashboard **NEXRAD — SCIT Core**.

`detection_v2` is **vendored** (`identify.py`, `track.py`, `types.py`) alongside a
minimal `GriddedVolume` and `DetectionParams`. SCIT only touches `site`,
`valid_time`, `reflectivity`, `x/y/z`, `dx_km`, `dz_km`, `xy_to_lonlat` — so the
container carries none of the desktop harness's GUI dependencies.

### Gridding: pyart was the wrong tool, and not mainly for speed

Measured on one KEVX volume:

| Route | Time |
|---|---|
| `pyart.xradar.Xradar` (lazy datatree) + grid | 110.99 s + 15.08 s = **126.07 s** |
| same, after `.load()` | 32.79 s + 14.89 s = **47.68 s** |
| **direct numpy max-bin (`scit/grid.py`)** | **0.59 s** (~80×) |

The `.load()` finding matters on its own — eager loading costs 1.1 s and saves
78 s, because lazy access through a ZipStore becomes thousands of tiny reads.

But **fidelity was the real problem**:

| | pyart Barnes2 | direct max-bin |
|---|---|---|
| composite max | **16.2 dBZ** | **46.5 dBZ** |
| "valid" cells | 85.5 % | 23.6 % |

Barnes2 with a range-dependent radius of influence smeared sparse gates across
large volumes, **crushing peak reflectivity below `seed_dbz = 40`** while
inventing echo where there was none. SCIT would never have seeded a single cell.
Max-binning preserves cores and leaves real gaps empty. (This is the right trade
for *identification*; render display products separately.)

Beam geometry uses the standard 4/3-effective-earth model — ignoring refraction
would misplace gates vertically by hundreds of metres at range, which is exactly
the axis `echo_top_min_km` and `continuity_levels` gate on.

### The gates work — demonstrated, not asserted

Same real KEVX volume, three parameter sets:

| Params | Cells |
|---|---|
| default (`seed 40 dBZ`) | **0** |
| `seed 25 dBZ` | **0** — gates still reject |
| `seed 25 dBZ`, **gates disabled** | **39** |

The 39 that appear with gates off look like this:

```
46.5 dBZ  top 1.5 km  depth 1.0 km  lv2
32.5 dBZ  top 0.5 km  depth 1.0 km  lv1
32.5 dBZ  top 0.0 km  depth 0.5 km  lv1
```

**Those are the sub-kilometre cells that took the old system offline.**

A second case is even clearer. One volume showed a **56.0 dBZ composite max and
still produced zero cells** — which could equally be correct rejection or a
silent bug, so it was checked:

```
cells >= 40 dBZ          : 3
their heights AGL        : min 1.50 km, max 1.50 km
distinct levels occupied : 1        (gate needs >= 3)
echo top (18.3 dBZ)      : 2.50 km  (gate needs >= 3.0 km)
gates disabled           -> 1 cell: 56.0 dBZ, top 2.50 km, lv1
```

A 56 dBZ return confined to a **single 500 m layer** — ground clutter or AP. The
old system would have called it a storm and handed it to TorNET.

**Zero cells in clear air is the correct output, and the dashboard says so** —
the "Composite max dBZ vs cells admitted" panel pairs the two so strong-echo /
no-cell reads as healthy, while cells appearing at low composite dBZ reads as
alarming.

### Performance

| Stage | mean |
|---|---|
| fetch + open (MinIO → in-memory datatree) | 1.9 s |
| grid (5.2 M gates → 31×301×301) | 0.6 s |
| identify + track | 0.03–0.3 s |
| persist | ~0 s |
| **total** | **~2.6 s/volume** |

### Persistence

`scit_cells` in the wxalerts TimescaleDB — one row per cell per volume with the
envelope as `geography(Polygon,4326)`, indexed on `(site, track_id, valid_time)`
for the per-track history the comparator needs, plus a GiST index on the
envelope. Every row carries `settings_hash`, so a detection is always traceable
to the exact knob set. Promoted to a hypertable when Timescale is available.

### Tracking is per site, deliberately

`Tracker` is greedy nearest-neighbour in **radar-relative metres**, so one
tracker spanning both sites would associate storms across two different
coordinate origins. Cross-site association — the same physical storm seen by
KMOB and KEVX — belongs above this stage and **is not implemented yet**.

### Gotcha

`docker compose up -d <svc>` reuses a cached image; the announce-to-`nexrad:zarr`
change appeared in the source but not in the running container, and scit sat idle
with an empty stream. Use `--build` (or `build --no-cache`) after editing.
