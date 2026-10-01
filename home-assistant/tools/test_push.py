#!/usr/bin/env python3
"""Send a labelled test push down the SAME path the lightning alerts use.

Deliberately reuses the real `data` payload -- Android `channel: lightning` +
`importance: high`, iOS `interruption-level: time-sensitive`. A plain test
notification would prove only that the phone is online; it would say nothing
about whether a 2 a.m. seek-shelter alert actually gets past Do Not Disturb and
Doze, which is the thing worth knowing.

Wording is unmistakably a test, so nobody reacts to it as a real strike.
"""
import json
import os
import urllib.error
import urllib.request

URL = os.environ["HA_URL"].rstrip("/")
TOK = os.environ["HA_TOKEN"]
HDR = {"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"}

TITLE = "TEST — lightning alerts are live"
MSG = ("This is a TEST of the GOES-19 GLM lightning alerts. No lightning is "
       "near you. Real alerts fire within 10 mi and look like this. "
       "Sent from the same high-priority channel, so if this got through "
       "silently the real one will too.")

SENDS = [
    ("notify.mobile_app_pixel_10_pro", "Clint / Pixel 10 Pro",
     {"channel": "lightning", "importance": "high", "priority": "high",
      "ttl": 0, "notification_icon": "mdi:flash-alert", "color": "#ffb300"}),
    ("notify.mobile_app_iphone", "Sierra / iPhone",
     {"push": {"interruption-level": "time-sensitive", "sound": "default"}}),
]

for service, who, extra in SENDS:
    domain, name = service.split(".", 1)
    body = {"title": TITLE, "message": MSG, "data": extra}
    req = urllib.request.Request(f"{URL}/api/services/{domain}/{name}",
                                 data=json.dumps(body).encode(),
                                 headers=HDR, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25) as f:
            print(f"  {who:<22} {service:<36} HTTP {f.status}")
    except urllib.error.HTTPError as e:
        print(f"  {who:<22} {service:<36} FAILED {e.code}: "
              f"{e.read().decode()[:200]}")

print("\nHA accepting the call means it handed the push to FCM/APNs. Only the "
      "handsets can confirm it actually arrived — check both.")
