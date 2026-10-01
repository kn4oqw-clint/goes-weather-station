#!/usr/bin/env python3
"""Export the installed lightning automations from HA into repo YAML.

Exporting beats hand-maintaining a parallel copy: the file then cannot drift
from what is actually running, which is the usual way a repo's "the config"
quietly stops being the config.
"""
import json
import os
import sys
import urllib.request

import yaml

URL = os.environ["HA_URL"].rstrip("/")
TOK = os.environ["HA_TOKEN"]
IDS = ["lightning_near_clint", "lightning_near_sierra", "lightning_near_home",
       "lightning_all_clear_home", "lightning_feed_stale"]

HEADER = """\
# Lightning alert automations — GOES-19 GLM proximity
# ===================================================
# EXPORTED FROM THE LIVE HOME ASSISTANT (10.10.0.5) — do not hand-edit and
# expect it to take effect. Round-trip is:
#     verification/install_lightning.py   repo -> HA
#     verification/export_automations.py  HA   -> repo
#
# Installed 2026-07-27. Consumes entities published by glm_lightning.py on the
# wxstation Pi; see docs/lightning-glm.md for the data path.
#
# POLICY (owner's choice): each person is alerted on their OWN phone when
# lightning comes within 10 mi of them; a strike near the house notifies the
# whole household.
#
# DEVICE MAP — verified against person.device_trackers, not assumed:
#     person.clint_chance  -> device_tracker.pixel_10_pro -> notify.mobile_app_pixel_10_pro
#     person.sierra_chance -> device_tracker.sierra ("iPhone") -> notify.mobile_app_iphone
#   device_tracker.clint_s_phone and device_tracker.sm_s918u exist but are NOT
#   linked to any person, so they are deliberately not targeted. `notify.notify`
#   is avoided for the same reason — it would broadcast to those two as well.
#
# WHY THE LEGACY notify.mobile_app_* SERVICE, not notify.send_message:
#   only it carries the delivery hints that matter for a seek-shelter alert —
#   Android `importance: high` on its own channel, iOS `interruption-level:
#   time-sensitive`. Those are what get a 2 a.m. warning past Do Not Disturb and
#   Doze. `send_message` accepts title/message only.
#
# WHY EVERY ALERT TRIGGER HAS `from: "off"`:
#   the service publishes `unavailable` when the GLM feed goes stale. An
#   unavailable -> on transition is the feed RECOVERING, not news about the sky.
#   Without the guard, every ingest hiccup would fire a false lightning alert.
"""


def get(aid):
    r = urllib.request.Request(f"{URL}/api/config/automation/config/{aid}",
                               headers={"Authorization": f"Bearer {TOK}"})
    with urllib.request.urlopen(r, timeout=20) as f:
        return json.load(f)


out = [get(i) for i in IDS]
dst = sys.argv[1]
with open(dst, "w") as f:
    f.write(HEADER + "\n")
    yaml.safe_dump(out, f, sort_keys=False, allow_unicode=True, width=100)
print(f"exported {len(out)} automations -> {dst}")
