#!/usr/bin/env python3
"""Insert the Leaflet map card into the weather-station dashboard, directly after
the existing GOES camera card. Idempotent (keyed on the URL) and backs up the
live config before writing."""
import json, os, sys, time, importlib.util

spec = importlib.util.spec_from_file_location("ha_ws", "/tmp/ha_ws.py")
hw = importlib.util.module_from_spec(spec); spec.loader.exec_module(hw)

DASH = "weather-station"
# Same origin as HA. An http:// URL here is blocked as mixed content whenever HA
# is opened at https://home.thechance.family (which is how it is normally reached
# -- NPMplus 301s http to https). Served via the /goesmap location on that host.
URL = "https://home.thechance.family/goesmap/map.html"
OLD_URLS = ["http://10.10.0.11:8099/map.html"]
ANCHOR = "camera.goes19_ir_loop"
CARD = {"type": "iframe", "url": URL,
        # Sections layout sizes by grid units, not aspect ratio; match the camera
        # card's full-width 36 columns and give it real height so the map is usable.
        "grid_options": {"columns": 36, "rows": 10}}

env = hw.load_env()
ha = hw.HA(env["HA_URL"], env["HA_TOKEN"])
cfg = ha.cmd(type="lovelace/config", url_path=DASH)

# include pid: two runs in the same second otherwise overwrite each other,
# which destroys the pre-change snapshot you actually wanted to keep.
bak = "/tmp/lovelace_%s_%s-%d.bak.json" % (DASH, time.strftime("%Y%m%d-%H%M%S"), os.getpid())
json.dump(cfg, open(bak, "w"), indent=1)
print("backup ->", bak)

# Migrate an earlier http:// card in place rather than adding a second one.
migrated = False
for v in cfg.get("views", []):
    for sec in v.get("sections") or []:
        for c in sec.get("cards") or []:
            if c.get("type") == "iframe" and c.get("url") in OLD_URLS:
                c["url"] = URL; migrated = True
if migrated:
    ha.cmd(type="lovelace/config/save", url_path=DASH, config=cfg)
    print("migrated existing card to", URL); sys.exit(0)
if json.dumps(cfg).count(URL):
    print("card already present; nothing to do")
    sys.exit(0)

placed = None
for vi, v in enumerate(cfg.get("views", [])):
    for si, s in enumerate(v.get("sections") or []):
        cards = s.get("cards") or []
        for ci, c in enumerate(cards):
            if c.get("entity") == ANCHOR or c.get("camera_image") == ANCHOR:
                cards.insert(ci+1, CARD)
                s["cards"] = cards
                placed = "view[%d].section[%d].card[%d]" % (vi, si, ci+1)
                break
        if placed: break
    if placed: break

if not placed:
    sys.exit("anchor card %s not found - refusing to guess a location" % ANCHOR)

ha.cmd(type="lovelace/config/save", url_path=DASH, config=cfg)
print("inserted at", placed)
