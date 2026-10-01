# GOES-19 Derived Products

Quantitative products sampled from GOES-19 ABI imagery over a box centred on the
station, published to Home Assistant over MQTT.

Built 2026-07-26. Service: `goes-derive.service` on the goes-proc LXC
(`10.10.0.11`), script `/usr/local/bin/goes_derive.py`, config
`/root/.goes_derive.env` (mode `0600`).

---

## The overlay problem — why there are two image handlers

`/etc/goesproc.conf` now has **two** image handlers for `goes19`:

| Handler | Output | Purpose |
|---|---|---|
| display (original) | `/srv/goes/goes19/…` JPEG **with** coastline/state overlays | the IR loop, dashboards, archive |
| raw (added 2026-07-26) | `/srv/goes/raw/goes19/…` **PNG, no overlays**, `json = true` | measurement |

The display product is unusable for measurement, and not subtly so:

* goesproc **burns the map overlays into the pixels**, saturating them to **255**
* on this 8-bit scale 255 is *also* the value of the coldest deep-convection
  tops (see the LUT comment in `make_loop.py`)
* the station sits on the **coast**, so the coastline runs straight through the
  sample box

Measured proof, sampling the display JPEG over the station:

```
cloud_cover_pct     12.3        <- mostly clear
cloud_top_index     100.0       <- "saturated deep convection"
cloud_top_temp_c    -85.0       <- ...on a 12% cloudy night
```

The 99th-percentile was reading the coastline. JPEG blocking compounds it by
smearing values at cloud edges. **Never point `goes_derive.py` at
`/srv/goes/goes19/`.**

The raw handler is restricted to `regions = ["fd"]` and
`channels = ["ch02","ch07","ch13","ch15"]` to bound disk; `goes-prune` already
sweeps all of `/srv/goes` at 24 h so it needs no separate cleanup.

---

## Geometry

Reuses the GOES-R fixed-grid maths from `make_loop.py`, generalised so the
per-pixel angular scale is derived from the image dimension. That means the same
code handles 2 km (5424²) `ch13`/`ch07`/`ch15` and a 1 km or 0.5 km `ch02`
without separate constants.

Sampling is a `101 × 101` lat/lon grid spanning ±`GOES_SAMPLE_KM` (default 50 km)
around the station, mapped to image pixels, with off-disk points masked.

**Verified** by cropping the sample region out of a display frame and looking at
it: Mobile Bay, the Gulf coastline, and the MS/AL and AL/FL borders all appear
where they should. Station pixel = `(col 2157, row 1147)`. Re-run that check
after any geometry change — a projection error produces plausible-looking
numbers from the wrong place on Earth.

---

## Calibration — real, from the HRIT metadata

Setting `json = true` on the raw handler paid off immediately: **the sidecars
carry the actual calibration tables**, so nothing here is guesswork.

```jsonc
// GOES19_FD_CH13_*.json
"ImageDataFunction": {
  "Map":   { "_NAME": "toa_brightness_temperature", "_UNIT": "K" },
  "Table": [ 340.35, 339.37, ... , 90.60, 89.62 ]     // 256 entries, count -> K
}
```

| Band | `_NAME` | `_UNIT` | Range |
|---|---|---|---|
| ch13 / ch07 / ch15 | `toa_brightness_temperature` | `K` | 340.35 → 89.62 K |
| ch02 | `toa_lambertian_equivalent_albedo_multiplied_by_cosine_solar_zenith_angle` | `1` | 0 → 1.295 |

So every temperature published is a **real calibrated brightness temperature**,
and `fog_btd_k` / `split_window_k` are **genuine brightness-temperature
differences in kelvin** — the standard products — rather than arbitrary indices.

The sidecars also confirmed the projection constants independently:
`x_add_offset = -0.151843995 rad`, `x_scale_factor = 0.000056 rad`, matching
what `make_loop.py` had hardcoded.

**ch02 is reflectance, not temperature.** `load_lut()` returns the unit and
`sample()` accepts only `K`, so the visible band cannot be silently mistaken for
a thermal one. `sample_albedo()` handles it separately, and since the table is
albedo × cos(solar zenith) — and cos(SZA) = sin(elevation) — dividing that out
recovers true albedo. Returns `None` below 5° elevation where the division blows
up.

If a sidecar is ever missing, the code logs a warning and falls back to a linear
approximation rather than silently emitting wrong numbers — watch for the
`Calibrated` diagnostic binary sensor going `off`.

### Cloud detection is self-calibrating

The 95th-percentile warmest pixel in the box estimates clear-sky/surface
brightness temperature (`clear_sky_temp_c`); anything more than `CLOUD_DELTA_K`
(8 K) colder is cloud. That adapts to season and time of day with no fixed
threshold to re-tune.

**Caveat:** under total overcast the 95th percentile *is* cloud, so cover is
under-reported. The coldest-top, cooling-rate and BTD products are unaffected.

---

## Published entities — HA device "GOES-19 Derived"

| Entity | Notes |
|---|---|
| `Cloud Cover` % | pixels > `CLOUD_DELTA_K` colder than the clear-sky estimate |
| `Cloud Top Temp` °C | 1st-percentile coldest — percentile, not min, so one bad pixel can't spike it |
| `Clear Sky Temp` °C | 95th-percentile warmest — the self-calibration reference |
| `Mean Brightness Temp` °C | box mean |
| `Cloud Top Cooling` K/30 min | positive = tops cooling = growing convection |
| `Fog BTD (10.3−3.9)` K | classic nighttime fog/low-stratus product |
| `Split Window (10.3−12.3)` K | low-level moisture |
| `Visible Albedo` | true TOA albedo, cos(SZA) divided out; `None` at night |
| `Solar Elevation` ° | computed geometry, not imagery |
| `Solar Index` % | clear fraction × sin(elevation) |
| `Frame Age` min | diagnostic — staleness of the newest ch13 frame |
| `Convective Initiation` binary | cooling ≥ `CI_RATE_K_30MIN` (4 K/30 min) |
| `Deep Convection` binary | cloud top ≤ `DEEP_CONVECTION_C` (−50 °C) |
| `Calibrated` binary | **diagnostic** — `off` means a sidecar was missing and values are approximations |

### Measured before/after — why the overlay handler mattered

Same location, same night, display JPEG vs overlay-free PNG:

| | Display JPEG | Raw PNG (calibrated) |
|---|---|---|
| cloud cover | 12.3 % | **1.4 %** |
| cloud-top | index 100.0 → **−85.0 °C** | **+22.8 °C** |

The display sample was reading the burned-in coastline as a saturated
deep-convection top on an almost cloudless night.

### Daylight gating — a bug worth remembering

`solar_index` was first gated on visible brightness (`albedo > 15`). That failed:
at 02:30 UTC — **21:30 local, full dark** — the ch02 mean was still 22 counts,
so it reported an **88 % solar index in the middle of the night**.

It now uses computed **solar elevation** instead, which has no such failure mode.
Verified: −24.9° at 22:00 local, +78.4° at solar noon, +21.0° at 18:00 local.
Don't reintroduce a brightness threshold for day/night.

`solar_index = clear_fraction × sin(elevation)` — both cloud *and* low sun angle
suppress insolation, so a clear sky at sunrise correctly scores near zero.

---

## Cadence

Full Disk arrives on HRIT every **30 minutes** (`:00` and `:30`). The service
polls every 2 minutes but only sees new data twice an hour, so `cooling_rate` is
a 30-minute differential. That is coarse for convective initiation — the
research standard is ~4 °C/15 min on 1–5 min mesoscale data.

**Mesoscale (M1/M2) is not a reliable substitute**: the sectors follow whatever
NOAA finds significant, and HRIT only carries a ~14-minute subset. Observed
2026-07-27: M1 over the Upper Midwest, M2 parked on a hurricane. They *would*
likely cover a Gulf event — so opportunistic use is worth adding later, but not
something to depend on.

---

## Ideas / next steps

- Animated loops of the derived fields (cloud-top index, fog, split-window).
- Opportunistically use M1/M2 when a sector actually covers the station, for
  ~14-minute convective-initiation differencing.
- Parse the `json` sidecars for real calibration and replace `count_to_c()`.
- Correlate `solar_index` against the Enphase production data — GOES cloud cover
  is the standard input for short-term solar forecasting.
- Cross-check `cloud_top_index` against GLM flash rate ([lightning-glm.md](lightning-glm.md));
  cold tops plus rising flash rate is a much stronger convective signal than either alone.
