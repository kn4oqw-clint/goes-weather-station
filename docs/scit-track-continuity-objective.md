# SCIT objective: track continuity and envelope fidelity

**Status:** replaces the hazard-skill objective. Written 2026-07-27.

## Why the objective changed

The system does not make hazard calls. It does not say "a tornado is on the
ground at these coordinates" — the data does not support that claim, and the NWS
does not make it either. What it does is accumulate *reasons why a cell has
potential*: this cell has these traits, it has steadily grown in intensity and
area, and it is heading toward you.

That product has a hard prerequisite. The evidence chain — "steadily grown" —
only exists if a storm **keeps one identity long enough to accumulate a
history**. If identity breaks every time the storm does something significant,
there is no chain to report.

So POD/FAR/CSI against Local Storm Reports is demoted. It is a *hazard-skill*
metric, and it answers a useful sanity question ("are we finding real storms at
all?"), but it is not the objective. The objective is:

1. **Envelope fidelity** — does the polygon wrap the storm, or does it claim
   empty air?
2. **Track continuity** — does identity survive the storm's lifecycle?

## What was broken

### The envelope was a convex hull

`identify._footprint_polygon` returned `MultiPoint(pts).convex_hull`. A convex
hull *bridges across* concavities. Measured on synthetic shapes at 1 km grid
spacing (`smoke.py`):

| shape | method | IoU | excess area | false area |
|---|---|---:|---:|---:|
| hook echo (C-shape) | convex | 0.623 | **1.62×** | 808 km² |
| hook echo (C-shape) | concave | 0.960 | 1.05× | 68 km² |
| hook echo (C-shape) | **footprint** | **1.000** | **1.00×** | **0 km²** |
| bow echo | convex | 0.533 | **1.89×** | 1250 km² |
| bow echo | concave | 0.939 | 1.08× | 109 km² |
| bow echo | **footprint** | **1.000** | **1.00×** | **0 km²** |
| two disjoint cores | convex | 0.519 | 1.93× | 566 km² |
| two disjoint cores | **footprint** | **1.000** | **1.00×** | **0 km²** |
| compact blob (control) | convex | 0.978 | 1.03× | 22 km² |

The control matters: on a round storm the convex hull is nearly right, which is
why the fault stayed invisible. It fails **specifically on the shapes that carry
the signal** — the hook's inflow notch and the bow's trailing gap are declared
to be part of the storm, so every downstream cylinder samples clear air as if it
were the cell.

`scit/envelope.py` provides three methods. `footprint` traces the exact
cell-edge boundary with marching squares at the half-level, preserving holes and
disjoint parts; it is ground truth by construction. `concave` is
`shapely.concave_hull`, a smoothed wrap. `convex` is retained only so the
regression stays measurable.

### The tracker could not represent a lifecycle

`track.Tracker` is greedy nearest-neighbour over seed centroids with a
`claimed` set enforcing strict 1:1, plus miss-count ageing. The 1:1 constraint
is structural, not a tuning gap — no parameter lets one track become two:

- a **split** gives one daughter the `track_id` and the other a brand-new track
  with **zero history**, destroying the intensification record at exactly the
  moment the storm does something worth alerting on;
- a **merge** lets one cell claim the track while the other goes unmatched, ages
  out, and **dies silently** — recorded as a disappearance.

It also tracked only `(x, y)`. "Shrank" and "grew a new high-dBZ core" were not
tracked at all.

Demonstrated on a synthetic storm that splits in two and merges back
(9 volumes, 5 min apart):

```
track_ids per volume, old tracker: [1] [1] [1] [1,2] [1,2] [1,2] [2] [2] [2]
```

The original storm's identity (track 1) is *gone* by the end. The surviving id
is the one created at the split. Orphan-birth rate 1.000, silent-death rate
1.000 — every lifecycle event was misrecorded.

## What replaces it

`scit/track2.py` — `OverlapTracker`. Association is by **advected footprint
overlap** rather than centroid distance, because overlap is what survives a
storm changing shape. The bipartite association graph is decomposed into
connected components, and each component is classified `1→1` continuation,
`1→N` split, `M→1` merge, or complex.

**Lineage is the durable identity.** `track_id` changes at a split, because
there are now genuinely two storms. `lineage_id` — the root ancestor — does not.
Every daughter inherits the parent's attribute history, so the evidence chain
survives. Same synthetic case:

```
lineages per volume, new tracker: [1] [1] [1] [1] [1] [1] [1] [1] [1]
```

| metric | old `Tracker` | `OverlapTracker` by lineage |
|---|---:|---:|
| identities | 2 | **1** |
| duration mean | 22.5 min | **40.0 min** (the whole run) |
| centroid RMS (L&S linearity) | 2.03 km | **0.16 km** |
| area RMS / mean | 0.180 | **0.032** |
| orphan births (unrecorded splits) | 1.000 | **0.000** |
| silent deaths (unrecorded merges) | 1.000 | **0.000** |
| lifecycle events recorded | none | `birth`, `split`, `merge` |

Attribute history lives on the track, so `TrackState.trend()` returns the growth
slopes directly — on the synthetic case, +11.0 km²/min area and +0.10 dBZ/min
across the full lifecycle including the split. That *is* the evidence chain the
comparator consumes.

## The metrics

`scit/trackmetrics.py` implements Lakshmanan & Smith (2010), *An objective
method of evaluating and devising storm-tracking algorithms*, Wea. Forecasting
25, 701–709 — which evaluates a tracker **without hand-labelled truth**:

1. **Duration** — tracks persist rather than fragmenting.
2. **Linearity** — RMS residual of centroid position about a best-fit line. An
   identity swap between two storms shows up here as a large residual even when
   duration looks healthy.
3. **Attribute consistency** — area, peak dBZ and echo top evolve smoothly.

### Both are individually gameable, so they are reported jointly

- **Duration alone** is maximised by associating everything to everything — so
  linearity and attribute consistency must sit beside it.
- **Linearity alone** is maximised by cutting every track into two-point
  segments, because a line through two points has exactly zero residual — so the
  linearity statistic is restricted to tracks of ≥3 samples and weighted by
  duration.

Never optimise one of these in isolation.

### Lifecycle correctness (added here, not in the paper)

The paper's metrics reward smooth tracks but say nothing about whether a split
was *recorded as a split*. Two diagnostics close that gap:

- **`orphan_births`** — a track born beside a storm that was already there and
  kept going. An unrecorded split, and the moment the new cell's growth history
  was thrown away.
- **`silent_deaths`** — a track ending beside a storm that continues. An
  unrecorded merge, reported as a disappearance.

A lineage that holds several cells in one volume (the daughters of a split) is
collapsed to a single family sample — area-weighted centroid, total area, peak
intensity — before scoring. Left alone, the daughters would enter the series as
consecutive samples and manufacture a position jump every volume.

## Measured on real radar (3 IEM warning cases, 10 contiguous volumes each)

### Envelope — 108 detected cells

| method | IoU | IoU p05 | excess | worst | enclosed area that is empty air |
|---|---:|---:|---:|---:|---:|
| convex (was default) | 0.778 | 0.583 | 1.36× | 2.19× | **39.0 %** |
| concave | 0.876 | 0.732 | 1.19× | 1.63× | 19.6 % |
| **footprint** | **1.000** | **1.000** | 0.98× | 1.02× | **0.3 %** |

Two fifths of what the convex envelope claimed as storm was clear air. The
`footprint` excess of 0.98 is the Douglas–Peucker simplification shaving
corners at a quarter of a grid cell; IoU stays exactly 1.000, so no cell is
gained or lost. Confirmed independently on tobac's objects: 792 cells,
IoU 1.000, excess 1.00.

### Detection density — the single-threshold detector cannot see MCS cores

| case | in-house (single-threshold) | tobac (7 thresholds) |
|---|---:|---:|
| ICT-7 (discrete supercell) | 11.2 cells/vol | 10.9 |
| LOT-25 (MCS) | 4.0 | **37.3** |
| MKX-42 (MCS) | 5.7 | **31.0** |

The two detectors agree almost exactly on the discrete supercell and diverge by
~8× on the MCS cases. That is the nested-threshold supersede rule resolving
embedded cores inside a squall line — the thing a single 40 dBZ seed contour is
structurally unable to do.

### Tracking — tobac wins on linearity; the singleton claim was an artifact

⚠️ **Correction.** The first version of this table reported tobac singleton
fractions of 0.048 / 0.120 / 0.123. Those were measured with tobac's default
`stubs=2`, which assigns `cell = -1` to any feature it cannot link into a
two-frame trajectory — and those features were then *excluded from scoring*.
That discards precisely the objects which would have counted as singletons. Re-run
at `stubs=1`, where every detection gets an id (which is what the streaming
service must do, since a new core has to be tracked the volume it appears in):

| | old `Tracker` | `OverlapTracker` (lineage) | tobac (cell, `stubs=1`) |
|---|---:|---:|---:|
| ICT-7 frac singleton | 0.475 | 0.519 | 0.412 |
| LOT-25 frac singleton | 0.600 | 0.316 | 0.587 |
| MKX-42 frac singleton | 0.548 | — | 0.638 |
| ICT-7 centroid RMS | 3.18 km | 3.78 km | **2.08 km** |
| LOT-25 centroid RMS | 3.00 km | 6.34 km | **1.75 km** |
| MKX-42 centroid RMS | 2.96 km | — | **2.20 km** |
| pooled dBZ RMS | 4.07 | 2.02 | **~1.4** |

On singleton fraction tobac is roughly a **wash** — better on two cases, worse
on one. It is not the reason to adopt it. Note though that tobac carries this
fraction over **9× more objects** on the MCS cases, so it is linking far more
cores at a comparable rate.

The reasons that survive scrutiny are **detection density** (8× on MCS, i.e.
actually resolving embedded cores), **centroid linearity** (clearly better in
all three cases, and computed over ≥3-sample tracks so it is unaffected by the
stubs setting), **attribute smoothness**, and **native family grouping**.

**The old tracker's own headline failure stands: 54 % of its tracks exist in
exactly one volume**, and it finds only 4–6 objects per volume on a squall line.

### The OverlapTracker's own failure, and what it taught

First real-radar run: centroid RMS 15.6 km, speed p95 **147.7 m/s**, 6
impossible jumps — far worse than the tracker it was meant to replace. Cause:
footprint-overlap association is *poisoned by bad segmentation*. Two 30,000 km²
single-threshold MCS blobs overlap substantially with centroids 100 km apart, so
the tracker linked them happily. A physical displacement cap was missing from
the association itself (it existed only on the motion estimate).

After adding the cap — reject any link implying more than `max_speed_ms`:

| pooled | before cap | after cap | old `Tracker` |
|---|---:|---:|---:|
| speed p95 | 147.7 m/s | **39.3 m/s** | 25.9 m/s |
| impossible jumps | 6 | **0** | 0 |
| centroid RMS | 15.62 km | **5.50 km** | 3.05 km |
| ids/storm-hour | 5.55 | **4.63** | 7.24 |
| frac singleton | 0.507 | **0.421** | 0.541 |
| dBZ RMS | 2.52 | **2.02** | 4.07 |

So the fixed `OverlapTracker` genuinely beats the old tracker on *continuity*
(fragmentation, duration, attribute smoothness) but remains worse on
*linearity*. **The lesson generalises: a tracker cannot be evaluated or fixed
independently of its segmentation.** Overlap association only becomes meaningful
once objects are storm-scale.

### Families: right idea, wrong scale (so far)

`merge_split_MEST` at 25 km collapses LOT-25's 100 trajectories into **2**
families spanning 50.4 min with zero orphan births and zero silent deaths — but
2 families for an entire squall line is too coarse to alert on, and the family
centroid teleports as membership changes (speed p95 55.8 m/s, RMS 8.16 km).
The cell level is the right *tracking* unit; the family level is a grouping
layer whose distance parameter still needs work.

### Caveat on the lifecycle diagnostics

`orphan_births` and `silent_deaths` use a fixed 20 km adjacency radius, which is
**density-sensitive**: at tobac's 37 cells/volume there is nearly always a
neighbour, inflating both rates (0.438 / 0.470 on LOT-25). Conversely the old
tracker scores a flattering 0.000 on LOT-25 precisely *because* 60 % of its
tracks are single-volume stubs with no surviving neighbours to be adjacent to.
Read these two numbers only alongside fragmentation and singleton fraction, and
scale the radius to object size before comparing across detectors.

## The sanity floor: are we finding real storms at all?

Detection skill is **not** the objective — the system does not make hazard calls
— but a detector that produced beautifully stable tracks of nothing would still
be broken. `verification/lsrverify.py` scores POD/FAR/CSI against IEM Local
Storm Reports (severe only: tornado, hail, wind gust, damage), 8 volumes per
case, 205 reports in range.

| level | reports | detected | objects | false alarms | POD | FAR | CSI |
|---|---:|---:|---:|---:|---:|---:|---:|
| cell | 205 | 174 | 629 | 537 | **0.849** | 0.854 | 0.235 |
| family | 205 | 168 | 127 | 82 | **0.820** | 0.646 | **0.585** |

**The floor is cleared: POD ≈ 0.85 at cell level and 0.82 at family level.** The
system is finding the storms that produced reports.

**The family is the better alerting unit, decisively.** It reaches essentially
the same detection rate (168 vs 174 of 205) with **five times fewer objects**
(127 vs 629), so CSI is 0.585 against 0.235. Cell-level FAR of 0.854 is the
price of resolving embedded cores — most cells match no report, which is exactly
what a multi-threshold detector should do on a squall line and exactly why cells
are the *tracking* unit rather than the *alerting* unit.

For scale, MRMS HSDA reports POD 0.594 / FAR 0.136 / CSI 0.543 against SHAVE.
Not a like-for-like comparison — SHAVE includes no-hail nulls and LSR does not —
but the family-level CSI is in the same territory.

### A methodological error worth recording

The first run scored objects against reports by **centroid distance** at 15 km.
That is defensible for a cell and completely wrong for a family: a complex
covering 35,000 km² has a centroid nowhere near any particular report. Measured
on one volume, the cell level detected 4/4 reports while the family level scored
**0/4** — for storms whose envelope plainly contained the reports.

Matching now tests **containment in the object's envelope**, with centroid
distance kept only as a near-miss allowance. The effect was large at both
levels (LOT-25 02:24Z: cell 10/19 → 17/19, family 3/19 → 17/19), because
containment also catches reports inside a cell's footprint but more than 15 km
from its seed.

## Calibrated grid: max binning measured, not assumed

Zhang et al. (2005) found maximum binning "would result in overestimation biases
from the VPR issue". That is a reason to measure, not to assume, so
`verification/binbias.py` grids the same sweeps both ways:

| case | volume | cells with >1 gate | mean bias | p95 | **bias where ≥40 dBZ** |
|---|---|---:|---:|---:|---:|
| ICT-7 (discrete) | 00:24Z | 39.7 % | 0.81 dB | 4.51 | **2.36 dB** |
| ICT-7 | 00:31Z | 39.7 % | 0.81 dB | 4.55 | **2.39 dB** |
| LOT-25 (MCS) | 02:17Z | 39.6 % | 1.77 dB | 5.71 | **4.08 dB** |
| LOT-25 | 02:24Z | 39.7 % | 1.79 dB | 5.73 | **4.10 dB** |
| MKX-42 (MCS) | 23:56Z | 39.6 % | 1.51 dB | 5.19 | **4.20 dB** |

About **40 % of grid cells receive more than one gate**, so the choice of
reducer is not academic. In the strong echo that VIL and MESH integrate, max
exceeds the mean by 2.4 dB on a discrete supercell and **4.1–4.2 dB on MCS
volumes**. 4.1 dB is a factor of 2.6 in linear Z; VIL scales as Z^(4/7), so that
is roughly a **70 % VIL overestimate**, and MESH inherits it.

`GriddedVolume` now carries a second field, `reflectivity_cal`: the **linear-Z**
mean of the gates in each cell. Averaging is done in linear Z and converted back
to dBZ at the end — dBZ is logarithmic, so a dBZ-space mean is not the mean of
the physical quantity VIL integrates and would understate exactly the strong
returns that matter.

`reflectivity` (max) is unchanged and still drives detection and display, where
preserving the peak is the point. **Nothing consumes `reflectivity_cal` yet** —
it is groundwork for the VIL/MESH cylinders, deployed now because the bias is
measured and the marginal cost is ~2 s per volume.

Verified in the deployed image: identical finite mask to the max grid, zero
cases of the mean exceeding the maximum, and bias figures reproducing the
independent measurement exactly.

## Consequence for the algorithm choice

**tobac moves ahead of hagelslag EWS.** Enhanced watershed fixes segmentation
only; tobac gives multi-threshold segmentation *plus* native split/merge-aware
tracking, and tracking is now the harder and more important half.

Over-segmentation is also worse than previously assessed. It is not merely a
false-alarm problem: a storm fragmenting into 60 objects has no stable identity,
so the evidence chain never accumulates at all. Under-segmentation loses
structure; over-segmentation loses **identity**. Both are fatal to the product.

## The two-level model (adopted 2026-07-27)

tobac is the SCIT core. Identity is a hierarchy, because a squall line is one
storm complex containing many cores and **both levels matter**:

**`family`** — the complex. A whole squall line may legitimately be one or a few
families, and that is the intent, not a defect. Its envelope is the union of its
members' ≥`base_dbz` footprints traced as a single mask, so it covers the
stratiform region rather than just the cores. Its history carries total area,
peak intensity and **cell count** — a complex spawning new cores is a signal in
its own right.

**`cell`** — a core inside a family, tracked from the volume it first appears
in, with its own area / intensity / echo-top history. A core that has been
intensifying for 20 minutes reads as such even when the family around it is
steady.

### Split daughters inherit their parent's record

Churn is expected and fine — cores form and die inside a complex constantly.
**Losing the record across a split is not.** When a cell or family splits, the
strongest claim keeps the uid and the other branch gets a new one, but it is
created *from its parent*: it inherits the parent's `history` **and** its `born`
time, and records `split_from`. So a core that breaks off a larger cell arrives
already carrying what it broke off from, and its `age_min` and trend columns
describe the parent storm rather than restarting the clock.

**A split is detected two ways, and the second one is the one that matters.**

1. *Re-link re-partition* — tobac splits a previously single trajectory between
   windows, so two groups contest one uid. The loser records the winner as its
   parent.
2. *Geometric split* — the physically important case. A genuine new core is a
   **brand-new feature with no prior link at all**, so it has no votes to
   contest and reconciliation alone would mint it with an empty history. It is
   instead found geometrically: a new object whose footprint falls
   ≥`split_overlap_min` (default 0.5) inside the footprint a parent occupied
   last volume — advected by that parent's own motion — *is* that parent
   splitting.

Measured with only mechanism (1) in place, splits detected across ICT-7 and
LOT-25 were **zero**. With the geometric test added:

| case | split daughters | arrived with inherited history |
|---|---:|---:|
| ICT-7 | 15 | **15 (100 %)** |
| LOT-25 | 87 | **87 (100 %)** |
| MKX-42 | 80 | **80 (100 %)** |

```
cell #12 split from #2:   3 inherited obs, age  7 min at birth
cell #13 split from #5:   4 inherited obs, age 14 min at birth
cell #50 split from #30:  3 inherited obs, age  7 min at birth
```

Inheritance also improved the tracking metrics as a side effect — ICT-7 cell
tracks fell 34 → 29, mean duration rose to 20.6 min, fragmentation fell
3.64 → 2.91 ids/storm-hour.

### The metric had to be fixed too

`orphan_births` was built to detect splits the *old* tracker could not express,
by flagging a birth beside a surviving neighbour. Once splits became explicit in
`split_from`, the metric could not see them and counted every **recorded** split
as an orphan — so it read *worse* (ICT-7 cell-level 0.600) precisely because the
problem had been fixed. A metric that degrades when the system improves is
actively misleading.

`_lifecycle` now separates the two. A birth adjacent to a survivor is a split
either way; the question is whether it was recorded:

* **`split_coverage`** — of the births that look like splits, the fraction
  recorded as one (and so inheriting the parent's history). **Higher is better**;
  this is the number to read.
* **`orphan_births`** — the remainder, splits that were *not* recorded. Still
  lower-is-better, and now it means what it says.

### Streaming: tobac links in batch, production does not

`linking_trackpy` and `merge_split_MEST` operate over a complete time series.
`StormHierarchy` runs a **rolling window** — the last 12 volumes are re-linked on
every new volume — then reconciles the ephemeral batch ids to persistent uids by
majority vote over the frames the new window shares with the previous one.

* Only the newest volume is emitted and persisted, so re-linking never rewrites
  history that is already written.
* `stubs=1` is deliberate: tobac's default `stubs=2` leaves a newly formed core
  unassigned until its second volume, and a new core must be tracked the moment
  it is seen. **This is also the setting the metrics must be compared at** — see
  the correction above.
* Cost: ~0.5 s detection + 2–7 s linking per volume, well inside cadence.

### Streaming results, 10 contiguous volumes per case

| | ICT-7 | LOT-25 | MKX-42 |
|---|---:|---:|---:|
| dominant family held one id for | 63 min | 60 min | 58 min |
| family envelope IoU vs its members | **1.000** | **1.000** | **1.000** |
| family orphan births | 0.000 | 0.000 | 0.000 |
| family silent deaths | 0.000 | 0.043 | 0.077 |
| envelope continuity (volume-to-volume IoU) | 0.557 | 0.507 | 0.446 |
| family membership churn | 0.380 | 0.720 | 0.704 |

MKX-42 consolidated to a single family of 25–32 cells covering ~35,000 km²,
tracked unbroken for 58 minutes with a continuous growth curve.

**Membership churn is expected** — cores form and die inside a complex
constantly. It is recorded, not suppressed; the inheritance rule above is what
keeps churn from costing history.

**Family centroid linearity is the wrong metric** and was replaced by
volume-to-volume envelope IoU. A complex that absorbs a neighbour legitimately
moves its centroid a long way in one volume and would be penalised for doing the
physically correct thing.

### Family distance: 50 km, set by sweep

`merge_split_MEST(distance=...)` decides how aggressively cells are grouped into
a complex. `verification/famsweep.py` grids and detects once, then sweeps only
the grouping, over the two contrasting regimes:

Families per volume, by regime:

| distance | ICT-7 (discrete) | LOT-25 (MCS) | MKX-42 (MCS) |
|---:|---:|---:|---:|
| 10 km | 10.0 | 29.4 | 25.2 |
| 25 km *(was default)* | 5.9 | 5.2 | 4.8 |
| **50 km** | **2.6** | **1.4** | **1.9** |
| 100 km | 1.2 | 1.0 | 1.6 |
| 200 km | 1.0 | 1.0 | 1.0 |

Envelope continuity (volume-to-volume IoU) over the same sweep:

| distance | ICT-7 | LOT-25 | MKX-42 |
|---:|---:|---:|---:|
| 10 km | 0.508 | 0.246 | 0.316 |
| 25 km | 0.586 | 0.499 | 0.410 |
| **50 km** | 0.684 | 0.742 | 0.561 |
| 100 km | 0.737 | 0.848 | 0.601 |
| 200 km | 0.764 | 0.848 | 0.851 |

**50 km is the largest distance that still separates discrete storms.** At 25 km
a squall line fragments into ~5 families, contradicting the intent that a whole
squall be one or a few. At 100 km the discrete case collapses to 1.2
families/volume and separate storms stop being separate.

⚠️ **Envelope continuity cannot pick this parameter on its own** — it improves
monotonically all the way to 200 km, where the entire domain is one family.
"Group everything together" is trivially stable in exactly the way "associate
everything" trivially maximised track duration. This is the third time a
single-metric optimum has turned out to be degenerate in this project; the
binding constraint here is the *discrete* regime, not the MCS one.

## Files

| file | role |
|---|---|
| `scit/envelope.py` | footprint / concave / convex envelopes + fidelity scoring |
| `scit/track2.py` | `OverlapTracker` — split/merge-aware, lineage, attribute history |
| `scit/trackmetrics.py` | L&S-2010 suite + lifecycle diagnostics |
| `trackverif.py` | harness over contiguous volume sequences |
| `smoke.py` | synthetic geometry + lifecycle test, no download required |

`DetectionParams` gains `envelope_method` (default `footprint`),
`envelope_concave_ratio` and `envelope_simplify_frac`. `StormCell` gains
`envelope_xy` (the envelope in radar x/y metres, which is where overlap
association operates) and `lineage_id`.

## Caveat

Three IEM cases remain far too small a sample to generalise from. The synthetic
results above are exact by construction; the real-radar figures are indicative
and need a much wider case set before any parameter is locked.
