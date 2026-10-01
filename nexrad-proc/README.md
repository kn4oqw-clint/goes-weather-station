# nexrad-proc — SCIT storm identification and tracking

Source for the storm-cell identification and tracking that runs on the
`nexrad-proc` VM (10.10.0.68, `/opt/nexrad/scit`). This directory holds the
modules that changed when the objective moved from hazard skill to **track
continuity and envelope fidelity** — see
[`docs/scit-track-continuity-objective.md`](../docs/scit-track-continuity-objective.md)
for why, and for the measured results.

## `scit/`

| module | role |
|---|---|
| `tobac_detect.py` | **the detector.** tobac 7-threshold multi-scale detection (Johnson et al. 1998 ladder) with the existing 3-D admission gates applied per object. Replaces `identify`, which could not resolve cores embedded in a squall line. |
| `hierarchy.py` | **the tracker.** Two-level family/cell identity over a rolling window, with per-node history and trends, and split daughters inheriting their parent's record. Replaces `track.py`. |
| `envelope.py` | footprint / concave / convex envelopes plus fidelity scoring. Replaces the convex hull, which bridged across hooks and bow-echo notches and claimed the empty air as storm. |
| `trackmetrics.py` | Lakshmanan & Smith (2010) duration / linearity / attribute-consistency, plus orphan-birth and silent-death lifecycle diagnostics. |
| `identify.py` | superseded by `tobac_detect.py`; kept as the measured baseline. Patched to use `envelope.py`. |
| `track2.py` | `OverlapTracker` — superseded by tobac, kept as the measured baseline. Its failure mode is documented: footprint-overlap association is poisoned by bad segmentation. |
| `types.py` | `StormCell` gains `envelope_xy`, `lineage_id`, optional footprint retention. |
| `params.py` | `envelope_method` (default `footprint`), `envelope_concave_ratio`, `envelope_simplify_frac`. |
| `main.py` | the service, rewritten for two-level persistence (`scit_families`, `family_id`/`split_from`/age/trend columns) and two-level announce. |

`grid.py` and `models.py` are unchanged and live only on the VM.

## Deployment status

**Deployed to `nexrad-proc` (10.10.0.68) on 2026-07-27.** The service runs the
tobac path; settings hash `f1cbd4355e558bae`. Admission gates unchanged
(`seed=40 dBZ`, `echo_top_min=3.0 km`, `levels=3`).

Schema migrated in place, existing rows preserved:

| | before | after |
|---|---:|---:|
| `scit_cells` columns | 16 | 22 |
| envelope type | `geography(Polygon)` | `geography(Geometry)` |
| `scit_families` | absent | created |

Two build/deploy traps, both hit and fixed — worth not repeating:

* **`libexpat1` must be installed AFTER the `build-essential` purge.** It is a
  runtime dependency of tobac (iris → cf_units → udunits2), and the purge's
  `autoremove` strips it back out, leaving `import tobac` dead with
  `libexpat.so.1: cannot open shared object file`.
* **The index on `family_id` belongs in `MIGRATE`, not `DDL`.** On an existing
  database `CREATE TABLE IF NOT EXISTS` is a no-op, so the column does not exist
  when `DDL` runs and creating its index aborts the whole statement — the
  service crash-looped on `UndefinedColumn` until the index was moved.

Rollback: `/opt/nexrad/scit/{main.py,Dockerfile,requirements.txt}.bak` and
`scit.bak/` on the VM. The schema changes are additive plus one widening, so the
old code still runs against the migrated table.

Note `numba` is absent from the image; tobac warns about it but only uses it for
periodic-boundary calculations, and this pipeline runs `PBC_flag='none'`.

### Poison-message handling

A failure used to mean "do not ACK", so the entry stayed pending, `xautoclaim`
re-delivered it, and it failed again — **forever**. One truncated volume (xradar
raises `IndexError` on a short file) would hold a consumer slot permanently and
fill the log with the same traceback.

Retries are now bounded. `SCIT_MAX_DELIVERIES` (default 3) attempts, then the
unit is written to `nexrad:cells:dead` and ACKed so the group moves on;
`scit_poison_total{site}` counts drops. Bounded retries separate transient from
permanent failure without having to classify them — a MinIO blip succeeds within
the budget, a corrupt volume never will. The unit is preserved in the
dead-letter stream rather than discarded, so it stays inspectable.

Verified by injecting a unit pointing at a nonexistent object:

```
WARNING retry 2/3 for KPOISON ... (left pending)
ERROR   POISON KPOISON ... dropped after 3 deliveries -> nexrad:cells:dead
```

pending returned to 0. Retries are paced by `CLAIM_MIN_IDLE_MS` (10 min in
production), so the default budget is ~20 minutes of grace before a drop.

### Running verification harnesses

Run them as a **separate one-off container**, not inside `nexrad-scit`:

```bash
docker run --rm -d --name scit-sweep \
  -v /mnt/ingest:/mnt/ingest -v /tmp/scitv:/work -v /tmp/cases.json:/tmp/cases.json \
  -v /var/tmp/volcache:/var/tmp/volcache \
  nexrad-scit:latest sh -c "SCIT_SRC=/app python -u /work/famsweep.py > /work/famsweep.log 2>&1"
```

A `docker compose up -d scit` recreates the service container and silently kills
anything running inside it — which is exactly how the first family-distance
sweep was lost. Mounting `/var/tmp/volcache` from the host also keeps the volume
cache across restarts.

## `verification/`

| script | role |
|---|---|
| `smoke.py` | Synthetic geometry and lifecycle test. **No download required** — run this first, it is the fastest way to confirm the envelope and tracker still behave. |
| `trackverif.py` | The main harness. Runs contiguous volume sequences and scores envelopes (convex vs concave vs footprint) and trackers (strict 1:1 vs overlap) on the same volumes. |
| `tobac_eval.py` | tobac multi-threshold detection + `merge_split_MEST`, scored on the *same* metrics so it sits beside the in-house detector on the same axes. |
| `volcache.py` | Caches the 2-D column products (`colmax`, `etop_km`, `nlev_seed`) so re-runs cost seconds instead of ~30 s per volume. |

### Running

The science stack lives in the `nexrad-scit` container, not on the VM host:

```bash
# copy the tree in, then
docker exec nexrad-scit python /tmp/scitv/smoke.py
docker exec nexrad-scit sh -c "SCIT_SRC=/tmp/scitv python -u /tmp/scitv/trackverif.py --vols 10"
docker exec nexrad-scit sh -c "SCIT_SRC=/tmp/scitv PYTHONPATH=/tmp/pkgs python -u /tmp/scitv/tobac_eval.py --vols 10"
```

`tobac` and `hagelslag` are installed to `/tmp/pkgs` via `pip --target` rather
than into site-packages, so an evaluation dependency cannot perturb the numpy or
scipy the production service is running against. `tobac` additionally needs
`libexpat1` in the container for its `cf_units` import.

Cases come from `/tmp/cases.json` (IEM tornado warnings: ICT-7, LOT-25, MKX-42).

## Caveat

Three cases is far too small a sample to lock any parameter against. The
synthetic results in `smoke.py` are exact by construction; the real-radar
figures are indicative only.
