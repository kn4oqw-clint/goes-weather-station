# Context Transfer — NEXRAD/SCIT rebuild

State as of 2026-07-27. Written as a handoff for research into what's still
missing. Companion docs: [nexrad-rebuild-architecture.md](nexrad-rebuild-architecture.md),
[scit-revival-plan.md](scit-revival-plan.md), [lightning-glm.md](lightning-glm.md),
[goes-derived.md](goes-derived.md).

---

## 1. Where everything lives

| Thing | Address | Notes |
|---|---|---|
| `nexrad-proc` VM 151 | `ssh cchance@10.10.0.68` | Ubuntu 24.04, 12 c / 28 GB / 120 GB SSD, VLAN 1011 |
| Stack | `/opt/nexrad/docker-compose.yml` | redis, poller, ingest, scit, fusion |
| Secrets | `/opt/nexrad/.env` (0600) | MinIO creds + `SCIT_PG_DSN` |
| RAM buffer | `/mnt/ingest` | 8 GB tmpfs, in fstab |
| MinIO | `http://10.10.0.10` — **port 80, not 9000** | bucket `nexrad`, user `nexradsvc`, 3.1 TB free |
| Postgres | `172.16.50.211:5432` db `wxalerts` | tables `scit_cells`, `storms` |
| Grafana | `172.16.50.146:3000` folder `nexrad-pipeline` | dashboards `nexrad-poller/-ingest/-scit` |
| Prometheus | `172.16.50.146:9090` | jobs `nexrad-poller/-ingest/-scit`, hot-reloadable |
| Dashboard source | `monitoring/grafana/mk_nexrad_dashboards.py` | idempotent; `GRAFANA_TOKEN` env |

Host was remediated first: wazuh destroyed, **ZFS ARC capped at 16 GiB** (it had
grown to 66.5 GiB uncapped). Available RAM **21 → 81 GiB**; `local-lvm` 79 % → 50 %.

```
poller ─▶ nexrad:volumes ─▶ ingest ─▶ nexrad:zarr ─▶ scit ─▶ nexrad:cells ─▶ fusion ─▶ nexrad:storms
                                          │                      │                        │
                                     MinIO zarr             scit_cells               storms
```

---

## 2. What is actually proven vs merely built

### Proven with measurement

- **Poller** — 6.0 S3 calls/poll/site (vs 400+ naive), dedup verified over repeated polls, clear-air VCP gating live (reads VCP from the 2.4 kB `-S` chunk; ~1/4000th the bandwidth of finding out post-download).
- **Ingest** — 12.0 s/volume (dl 0.94 / decode 5.17 / zarr 5.56 / upload 0.32). **1 object per volume** via ZipStore vs 429 for a directory store. MinIO objects verified to read back as valid Zarr with correct dual-pol moments.
- **Gridder** — 0.59 s vs pyart's 47.68 s, *and* higher fidelity (pyart's Barnes2 crushed 46.5 dBZ peaks to 16.2 and inflated "valid" cells 24 %→85 %).
- **Admission gates reject shallow junk** — a 56 dBZ echo in a single 500 m layer with a 2.5 km top was correctly refused; with gates disabled it becomes a "cell". This is the class of failure that took the old system offline.
- **SCIT finds real storms** — ICT-7 tornado warning: cells inside the polygon in **8 of 8 volumes**, 71.5 dBZ, 13.5 km tops.

### Built but NOT proven

- **Cross-site fusion** — passes synthetic cases (fuses two sites into one storm, keeps a distinct storm separate, arbitrates to the nearer radar) but **has never run on real multi-site storms**, because SCIT's current parameters don't produce usable cells on the MCS cases.
- **Persistence** — `scit_cells` and `storms` schemas exist and write, but no query load has been run against them.
- **Poison-message handling** — absent. See §4.

### Disproven / broken

- **Stock SCIT segmentation parameters.** See §3. This is the blocker.

---

## 3. The open problem — SCIT under-segmentation

Validated against three IEM tornado warnings (April 2026), both nearest radars each:
ICT-7 (4083 km²), LOT-25 (3794 km²), MKX-42 (321 km², discrete).

With stock `seed_min_separation_km=6` / `watershed_min_sep_km=14`:

```
LOT-25  inside polygon: 65.0 dBZ max, 1429 grid cells >= 40 dBZ  ->  0 cells admitted
MKX-42  inside polygon: 61.0 dBZ max,  287 grid cells >= 40 dBZ  ->  0 cells admitted
ICT-7   inside polygon: 70.5 dBZ max, 1707 grid cells >= 40 dBZ  ->  5 cells admitted
```

LOT-25 admitted **3 cells for an entire MCS**. Sweep (summed over 3 cases):

| seed_sep / ws_sep / footprint_r | in_poly | cells | worst area km² |
|---|---|---|---|
| 6 / 14 / 25 (**stock**) | 5 | 20 | 1963 |
| 4 / 8 / 25 | 11 | 90 | 1965 |
| 3 / 6 / 25 | 14 | 271 | 1831 |
| 2 / 4 / 15 | 23 | 588 | 590 |

Every case improves as separation loosens — **but the loose end over-segments**:
305 cells from one KLOT volume is not 305 storms.

Also fixed along the way: added **`max_footprint_radius_km`** because `base_dbz=30`
let a single seed claim an entire MCS — **55,434 km² from KILX** against a 150 km
domain of only ~70,700 km². At r=25 km the worst case drops to 1,963 km² **with no
loss of detections**, whereas raising `base_dbz` to 40 shrinks areas but *costs* a
detection.

**Nothing has been locked in — and note the deployment gap.** The running
`scit-core` container is still on **stock parameters and has no footprint cap at
all**: `max_footprint_radius_km` exists only in the test tree (`/tmp/scittest` on
the VM) and in the scratch source, *not* in `/opt/nexrad/scit/`. Verified:

```
grep -c max_footprint_radius_km /opt/nexrad/scit/scit/params.py    -> 0
grep -c max_footprint_radius_km /opt/nexrad/scit/scit/identify.py  -> 0
```

So the pipeline currently running would reproduce the 55,434 km² envelope. The
fix and the tuning evidence are real; **deploying them is an outstanding action**,
deliberately not taken while the parameter choice is unsettled.

---

## 3b. RESEARCH ANSWERED THESE — corrections, 2026-07-27

Owner-supplied research settled §4's questions. These are **corrections to what
was built**, not refinements. Full detail in the `scit-algorithm-direction`
memory.

1. **Single-threshold seeding is structurally incapable.** Confirmed. No
   (seed, base, separation) tuple works for both regimes because they differ in
   *intensity structure*, not spacing. Operational SCIT uses **seven thresholds
   (30–60 dBZ)**, keeping per storm the highest threshold that yields a
   valid-size component — correct-identification **24 %→68 %** (>40 dBZ) and
   **41 %→96 %** (≥50 dBZ) over 6561 cells. **Stop tuning §3; replace the
   segmentation.** Adoptable: `hagelslag` enhanced watershed (Lakshmanan 2009) or
   `tobac` `feature_detection_multithreshold`.

2. **⚠️ `storm-fusion` is architecturally wrong.** MRMS merges 143 radars onto
   ONE 3-D grid with distance- and time-weighted exponential blending and runs
   detection **once** — grid-level fusion sidesteps object association entirely.
   The object-level association service built here should be **retired, not
   debugged**.

3. **Rotation is an independent detection input, not a cell attribute.** QLCS
   mesovortices (the LOT-25 miss) often have no reflectivity core. MRMS computes
   LLSD azimuthal shear independently of reflectivity. Allow **rotation-only
   objects**; QLCS Vrot 20–50 kt, ~30 kt operational trigger, smaller support
   than a supercell mesocyclone.

4. **Max-binning is wrong for calibrated products** — biases high via VPR and
   calibration, propagating into VIL/MESH. Use nearest-neighbour or narrow-radius
   Barnes/Cressman for calibrated fields; keep max-composite for display only.

5. **The objective function was degenerate.** Polygon counting monotonically
   rewards over-segmentation. Replace with LSR/SHAVE point truth (SHAVE uniquely
   has *no-hail nulls*) scored POD/FAR/CSI, plus MODE for split/merge diagnostics.
   Target: MRMS HSDA reached POD 0.594 / FAR 0.136 / CSI 0.543.

---

## 4. Research questions — what I'd go looking for

### 4.1 Is single-threshold seeding the right algorithm at all?

`detection_v2` seeds at one threshold (`seed_dbz=40`) and grows to one base
contour (`base_dbz=30`). **Operational SCIT (Johnson et al. 1998) uses seven
nested reflectivity thresholds** (roughly 30→60 dBZ) and builds cells from the
highest threshold that yields a component of valid size, which is precisely the
mechanism that separates embedded cores in an MCS. Our under-segmentation may be
a structural consequence of the single-threshold design rather than a tuning
problem — in which case no parameter set fixes it.

*Worth reading:* Johnson et al. 1998 (WAF) "The Storm Cell Identification and
Tracking Algorithm"; the WDSS-II / w2segmotionll hierarchical clustering approach
(Lakshmanan) which explicitly handles the nested-scale problem.

### 4.2 What is the right ground truth?

"Cells inside a tornado warning polygon" is a crude objective. Warnings are
human-drawn, deliberately over-warn, and cover a swath rather than a storm.
Verifying against them rewards over-segmentation — a detector emitting 300 cells
will always score more hits.

*Worth researching:* SPC **Local Storm Reports** (tornado/hail/wind) as point
truth; standard POD / FAR / CSI methodology for storm-scale object verification;
object-based verification (MODE) rather than point-in-polygon.

### 4.3 Should cell identification use velocity at all?

SCIT here is reflectivity-only. Tornadoes — especially QLCS tornadoes, which
LOT-25 looks like — frequently occur with unremarkable reflectivity but strong
rotation. `storm_modeler` already contains `couplets.py` (velocity couplets) and
`srm.py` (storm-relative motion) that are not wired in.

*Question to settle:* is rotation an *attribute of a cell* (current design — a
cylinder annotates cells SCIT found) or an *input to cell identification* (a
storm can be defined by rotation even without a reflectivity core)? The current
architecture assumes the former, and if that's wrong the whole cylinder concept
needs revisiting.

### 4.4 Cross-site fusion — is there prior art to copy?

NOAA's **MRMS (Multi-Radar Multi-Sensor)** system does exactly this operationally:
merges ~180 radars onto a common grid with time alignment and quality-weighted
blending. Our design (observation-to-track, Hungarian assignment on
IoU + distance + attributes, range-weighted arbitration) was derived from first
principles and has not been checked against how MRMS actually does it.

*Specifically worth knowing:* does MRMS merge at the **grid level** (blend
reflectivity fields, then detect once) rather than at the **object level** (detect
per radar, then associate)? Grid-level merging would sidestep the entire
association problem — and would be a materially different architecture from what
is built.

### 4.5 Gridding choices

- Grid is 1 km horizontal / 0.5 km vertical, 150 km radius, 15 km top.
- Max-binning per cell, then **vertical gap-fill by interpolation within each
  column** (see §5 — this was essential).
- Beam height uses the standard 4/3-effective-earth model.

*Open:* is max-binning the right reduction, or should it be a weighted mean with
a narrow radius of influence? Max preserves cores (good for seeding) but biases
reflectivity high, which matters if any cylinder consumes calibrated values.

---

## 5. Non-obvious facts worth not rediscovering

- **NOAA's volume counter rotates and wraps 999→001.** Never use it as identity;
  key from the timestamp in the chunk filename.
- **Chunk retention is disputed** — owner says 2 h; measured 52.6 h, `STANDARD`,
  retrievable. Design assumes short either way.
- **Archive bucket is `unidata-nexrad-level2`** (`noaa-nexrad-level2` = AccessDenied).
- **IEM CSV export carries no geometry** — the shapefile does (needs geopandas).
- **`.load()` a datatree before heavy access.** Lazy reads through a ZipStore cost
  78 s on one volume; eager loading costs 1.1 s.
- **Vertical gaps break 3D connectivity.** Radar cuts (0.48…19.51°) are spaced
  wider than a 0.5 km layer, so raw binning left one column occupying levels
  `[3,4,6,8,10,13,16,21,26]`. Result: **2499 of 2540 seed components (98 %)
  rejected on `continuity_levels`**, including a 56 dBZ core with an 8 km top at
  `lv=1`. `fill_vertical_gaps()` interpolates *within* a column only — filling
  past the ends would inflate `echo_top_km`, corrupting the gate it serves.
- **Zarr metadata dominates object count.** Sharding cut data chunks 1519→206 but
  a directory store still writes one `zarr.json` per array (223 of them). Only a
  single-object store fixes it.
- **`Counter("x_total")` collides with `Histogram("x")`** in prometheus_client.
- **Shard size must be an exact multiple of chunk size**, and sweeps differ in
  gate count (1832 vs 1192) — compute chunks as divisors per array.
- **`docker compose up -d <svc>` reuses the cached image.** Use `--build`.
- **A dataclass field with no type annotation is a class attribute**, not a field.
- **`ALERT_LIVE=true`** on the wxalerts celery worker — restoring the alert path
  resumes real pushes to 31 devices.

---

## 6. Known defects, unfixed

1. **Poison messages retry forever.** xradar throws `IndexError` on some archived
   volumes (`msg_31_header[self._group]`). `ingest` and `scit` return without
   `XACK` on failure, so `xautoclaim` re-delivers every 10 min indefinitely. Needs
   a retry counter and dead-letter stream.
2. **Cross-site association is not implemented in `scit-core`** — trackers are
   per-site by design; the same storm seen by two radars becomes two `track_id`s.
   `storm-fusion` is meant to resolve that but is unvalidated on real data.
3. **Five Prometheus jobs point at the decommissioned R820** (`100.73.32.52`) and
   have been `down` for a month.
4. **Output is 1.66× source size** — Level II is heavily bzip2'd; Zarr defaults to
   a faster codec and the zip is `ZIP_STORED`. Spare CPU exists to trade.
5. **`storms` table has no retirement/compaction policy.**

---

## 7. Not yet started

The engine cylinders and the comparator — the actual point of the system.

| Cylinder | Status |
|---|---|
| GLM lightning | **data already live** (`glm_flashes`, 19 s latency) — cheapest first |
| Cloud-top temp | GOES calibration solved ([goes-derived.md](goes-derived.md)) |
| NWS Alerts | `_encompassing_nws_id()` exists in the wxalerts matrix worker |
| HRRR / Overshoot | `detection/vault.py` written in storm_modeler |
| TGEN | `WxAlerts-tgen-proc` repo exists |
| TorNET | `wxalerts-tornet` repo exists |
| **Comparator** | does not exist — needs per-track attribute history, explainability, degradation-awareness, hysteresis |

Also unbuilt: **live NEXRAD radar in Home Assistant** (Zarr → TiTiler → Lovelace),
which is nearly free once the pipeline is trusted, and is the most visible win.
