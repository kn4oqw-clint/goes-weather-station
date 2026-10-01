#!/usr/bin/env python3
"""Install the GLM lightning automations into Home Assistant.

Policy (owner's choice): each person is alerted on their OWN phone when
lightning comes within 10 mi of them; a strike near the house notifies the whole
household.

Device mapping, verified against person.device_trackers rather than assumed:
    person.clint_chance  -> device_tracker.pixel_10_pro -> notify.mobile_app_pixel_10_pro
    person.sierra_chance -> device_tracker.sierra ("iPhone") -> notify.mobile_app_iphone

`notify.mobile_app_*` (legacy service) is used rather than `notify.send_message`
because only it carries the delivery hints that matter for a seek-shelter alert:
Android `importance: high` + its own channel, and iOS
`interruption-level: time-sensitive`. Those are what get a 2 a.m. lightning
warning past Do Not Disturb and Doze. `send_message` only takes title/message.

`from: "off"` on every alert trigger is deliberate. The service publishes
`unavailable` when the GLM feed goes stale, and an unavailable -> on transition
is not new information about the sky -- it is the feed coming back. Without the
`from` guard, every ingest hiccup would fire a false lightning alert.
"""
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ["HA_URL"].rstrip("/")
TOK = os.environ["HA_TOKEN"]
HDR = {"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"}

CLINT = "notify.mobile_app_pixel_10_pro"      # Android
SIERRA = "notify.mobile_app_iphone"           # iOS

ANDROID = {"channel": "lightning", "importance": "high", "priority": "high",
           "ttl": 0, "notification_icon": "mdi:flash-alert", "color": "#ffb300"}
IOS = {"push": {"interruption-level": "time-sensitive", "sound": "default"}}


def near(target, label):
    """Message body shared by the person and home alerts."""
    return (
        "{{ states('sensor.lightning_%s_strikes_10mi') }} strikes within 10 mi "
        "({{ states('sensor.lightning_%s_strike_rate') }}/min, "
        "{{ states('sensor.lightning_%s_trend') | lower }}). "
        "Seek shelter — stay inside 30 min after the last strike." % (
            target, target, target))


def title(target, prefix):
    return ("%s {{ states('sensor.lightning_%s_nearest_strike') }} mi away"
            % (prefix, target))


def alert_trigger(target):
    return [{"trigger": "state",
             "entity_id": f"binary_sensor.lightning_{target}_lightning_within_10mi",
             "from": "off", "to": "on"}]


AUTOMATIONS = [
    {
        "id": "lightning_near_clint",
        "alias": "Lightning — near Clint",
        "description": "GLM flash within 10 mi of Clint's tracked position.",
        "mode": "single",
        "triggers": alert_trigger("clint_chance"),
        "actions": [{"action": CLINT, "data": {
            "title": title("clint_chance", "⚡ Lightning"),
            "message": near("clint_chance", "Clint"),
            "data": ANDROID}}],
    },
    {
        "id": "lightning_near_sierra",
        "alias": "Lightning — near Sierra",
        "description": "GLM flash within 10 mi of Sierra's tracked position.",
        "mode": "single",
        "triggers": alert_trigger("sierra_chance"),
        "actions": [{"action": SIERRA, "data": {
            "title": title("sierra_chance", "⚡ Lightning"),
            "message": near("sierra_chance", "Sierra"),
            "data": IOS}}],
    },
    {
        "id": "lightning_near_home",
        "alias": "Lightning — near Home",
        "description": "GLM flash within 10 mi of the station. Whole household.",
        "mode": "single",
        "triggers": alert_trigger("home"),
        "actions": [
            {"action": CLINT, "data": {
                "title": title("home", "⚡ Lightning near the house —"),
                "message": near("home", "home"), "data": ANDROID}},
            {"action": SIERRA, "data": {
                "title": title("home", "⚡ Lightning near the house —"),
                "message": near("home", "home"), "data": IOS}},
            {"action": "persistent_notification.create", "data": {
                "notification_id": "lightning_home",
                "title": "⚡ Lightning near the house",
                "message": near("home", "home")}},
        ],
    },
    {
        # The 30-30 rule: it is not safe until 30 minutes after the LAST strike.
        # `for:` does the waiting, so this fires only once the sky has settled.
        "id": "lightning_all_clear_home",
        "alias": "Lightning — all clear (home)",
        "description": "Fires 30 min after the last strike inside 10 mi of the house.",
        "mode": "single",
        "triggers": [{"trigger": "state",
                      "entity_id": "binary_sensor.lightning_home_lightning_within_10mi",
                      "from": "on", "to": "off", "for": "00:30:00"}],
        "actions": [
            {"action": CLINT, "data": {
                "title": "✅ Lightning all clear",
                "message": "No strikes within 10 mi for 30 minutes. Safe to head back outside.",
                "data": {"channel": "lightning"}}},
            {"action": SIERRA, "data": {
                "title": "✅ Lightning all clear",
                "message": "No strikes within 10 mi for 30 minutes. Safe to head back outside."}},
            {"action": "persistent_notification.dismiss",
             "data": {"notification_id": "lightning_home"}},
        ],
    },
    {
        # This one matters more than it looks. GLM arrives over the INTERNET
        # from NOAA's S3 bucket, not off the dish, so the feed can die exactly
        # when a storm is overhead. The service reports `unavailable` rather
        # than a confident zero; this turns that into something you can see.
        "id": "lightning_feed_stale",
        "alias": "Lightning — GLM feed unavailable",
        "description": "GLM ingest stale; lightning data is NOT trustworthy.",
        "mode": "single",
        "triggers": [{"trigger": "state",
                      "entity_id": "binary_sensor.lightning_home_lightning_within_10mi",
                      "to": "unavailable", "for": "00:10:00"}],
        "actions": [
            {"action": CLINT, "data": {
                "title": "⚠️ Lightning feed down",
                "message": ("GLM data is stale — lightning detection is offline. "
                            "Do not treat \"no strikes\" as safe."),
                "data": {"channel": "lightning"}}},
            {"action": "persistent_notification.create", "data": {
                "notification_id": "lightning_feed_stale",
                "title": "⚠️ Lightning feed down",
                "message": ("GLM ingest is stale. Check glm-lightning.service on "
                            "the wxstation Pi (10.10.0.171) and "
                            "wxalerts-glm-ingest on 172.16.50.211.")}},
        ],
    },
]


def post(a):
    req = urllib.request.Request(
        f"{URL}/api/config/automation/config/{a['id']}",
        data=json.dumps(a).encode(), headers=HDR, method="POST")
    with urllib.request.urlopen(req, timeout=20) as f:
        return f.status, f.read().decode()[:200]


def main():
    dry = "--dry-run" in sys.argv
    for a in AUTOMATIONS:
        if dry:
            print(f"WOULD POST {a['id']}")
            continue
        try:
            st, body = post(a)
            print(f"  {a['id']:<28} HTTP {st} {body}")
        except urllib.error.HTTPError as e:
            print(f"  {a['id']:<28} FAILED {e.code}: {e.read().decode()[:300]}")
            sys.exit(1)
    if dry:
        return
    req = urllib.request.Request(f"{URL}/api/services/automation/reload",
                                 data=b"{}", headers=HDR, method="POST")
    with urllib.request.urlopen(req, timeout=20) as f:
        print(f"  reload -> HTTP {f.status}")


if __name__ == "__main__":
    main()
