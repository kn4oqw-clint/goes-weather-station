#!/usr/bin/env python3
"""Insert a Lightning section into the weather-station dashboard's "Now" view.

Read -> modify -> write the WHOLE config back; `lovelace/config/save` replaces
everything, so the backup taken first is the only undo.

Placement: immediately after "NWS Alerts", ahead of the GOES loop. Lightning
proximity is the one thing on this page you might have to act on in the next
five minutes, so it goes above the imagery, not below it.

Idempotent -- an existing Lightning section is replaced rather than duplicated,
so this can be re-run after edits.
"""
import json
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else "ws_dashboard_backup.json"
DST = sys.argv[2] if len(sys.argv) > 2 else "ws_dashboard_new.json"

TARGETS = [("home", "Home"),
           ("clint_chance", "Clint"),
           ("sierra_chance", "Sierra")]


def tiles(slug, label):
    """One row per target: the safety flag first, then distance and intensity."""
    return {
        "type": "glance",
        "title": label,
        "columns": 4,
        "state_color": True,
        "entities": [
            {"entity": f"binary_sensor.lightning_{slug}_lightning_within_10mi",
             "name": "Within 10mi"},
            {"entity": f"sensor.lightning_{slug}_nearest_strike",
             "name": "Nearest"},
            {"entity": f"sensor.lightning_{slug}_strikes_10mi",
             "name": "Strikes 10mi"},
            {"entity": f"sensor.lightning_{slug}_trend", "name": "Trend"},
        ],
    }


LIGHTNING_SECTION = {
    "type": "grid",
    "cards": [
        {"type": "heading", "heading": "Lightning (GOES-19 GLM)",
         "icon": "mdi:flash-alert"},

        # Only rendered when something is actually within 10 mi, so the page
        # stays quiet on a calm day and this is unmissable when it is not.
        {"type": "conditional",
         "conditions": [
             {"condition": "or", "conditions": [
                 {"condition": "state",
                  "entity": f"binary_sensor.lightning_{s}_lightning_within_10mi",
                  "state": "on"} for s, _ in TARGETS]}],
         "card": {
             "type": "markdown",
             "content": (
                 "## ⚡ LIGHTNING WITHIN 10 MILES\n"
                 "{% for t, n in [('home','Home'), ('clint_chance','Clint'), "
                 "('sierra_chance','Sierra')] %}"
                 "{% if is_state('binary_sensor.lightning_' ~ t ~ "
                 "'_lightning_within_10mi','on') %}"
                 "- **{{ n }}** — {{ states('sensor.lightning_' ~ t ~ "
                 "'_nearest_strike') }} mi, "
                 "{{ states('sensor.lightning_' ~ t ~ '_strikes_10mi') }} strikes, "
                 "{{ states('sensor.lightning_' ~ t ~ '_trend') | lower }}\n"
                 "{% endif %}{% endfor %}\n"
                 "\n**Seek shelter — stay inside 30 minutes after the last strike.**")}},

        # Shown only when the feed is stale. A confident "0 strikes" during an
        # internet outage is the dangerous failure mode, so absence of data has
        # to look different from absence of lightning.
        {"type": "conditional",
         "conditions": [{"condition": "state",
                         "entity": "binary_sensor.lightning_home_lightning_within_10mi",
                         "state": "unavailable"}],
         "card": {"type": "markdown",
                  "content": ("### ⚠️ Lightning feed unavailable\n"
                              "GLM data is stale — this is **not** the same as "
                              "no lightning. Do not treat a quiet page as safe.\n\n"
                              "_Check `glm-lightning.service` on the wxstation Pi "
                              "and `wxalerts-glm-ingest` on 172.16.50.211._")}},
    ] + [tiles(s, n) for s, n in TARGETS] + [
        {"type": "markdown",
         "content": ("_GOES-19 GLM via NOAA S3 — **internet-sourced**, not off "
                     "the dish. 10 mi threshold follows NWS "
                     "*When Thunder Roars* / the 30-30 rule. "
                     "Rate is flashes/min within 30 mi over a 30 min window._")},
    ],
}


def main():
    cfg = json.load(open(SRC))
    now = next(v for v in cfg["views"] if v.get("path") == "now")
    secs = now["sections"]

    def is_lightning(s):
        for c in s.get("cards", []):
            if c.get("type") == "heading" and "Lightning" in str(c.get("heading")):
                return True
        return False

    secs[:] = [s for s in secs if not is_lightning(s)]

    idx = 0
    for i, s in enumerate(secs):
        for c in s.get("cards", []):
            if c.get("type") == "heading" and "NWS Alerts" in str(c.get("heading")):
                idx = i + 1
    secs.insert(idx, LIGHTNING_SECTION)

    json.dump(cfg, open(DST, "w"), indent=2)
    print(f"inserted Lightning at section index {idx} of {len(secs)}")
    for i, s in enumerate(secs):
        h = next((c.get("heading") for c in s.get("cards", [])
                  if c.get("type") == "heading"), "?")
        print(f"   [{i}] {h}{'   <== NEW' if i == idx else ''}")


if __name__ == "__main__":
    main()
