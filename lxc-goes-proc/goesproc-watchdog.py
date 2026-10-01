#!/usr/bin/env python3
"""Guard the Pi->LXC packet stream. The failure this exists for (2026-07-28, 3
days of no imagery): the TCP connection carrying packets from goesrecv on the Pi
goes HALF-OPEN -- the accepted socket disappears on the Pi side while goesproc
keeps an ESTABLISHED socket that never receives another byte. nanomsg has no
keepalive here, so goesproc blocks on the dead socket forever.

Nothing on the Pi can detect this. goesrecv stays healthy the whole time (omega
steady, packets_ok flat at its normal rate, drops ~0), so goesrecv-watchdog.py
and the Grafana "packets rate == 0" alert both correctly report OK while zero
imagery is produced. The only place the fault is visible is HERE, on the
subscriber side.

Primary signal is the kernel's `lastrcv` for goesproc's socket -- milliseconds
since the last byte arrived. Packets flow continuously (~3400/min), so healthy
is single-digit ms; the broken socket read 279992433 (77.8 h). A secondary
product-freshness check backstops the case where the stream is fine but goesproc
stops writing (full disk, bad permissions).

Config via environment (see /etc/goes/ha.env):
  HA_URL     e.g. http://10.10.0.5:8123
  HA_TOKEN   HA long-lived token (only used to publish the health sensor)
"""
import os, re, json, time, subprocess, urllib.request

HA_URL      = os.environ.get("HA_URL", "")
HA_TOKEN    = os.environ.get("HA_TOKEN", "")
STALL_MS    = 120_000        # no bytes on the packet socket for this long -> dead stream
PRODUCT_AGE = 30 * 60        # no product written for this long -> goesproc wedged
GRACE       = 120            # let a freshly restarted goesproc connect before judging
COOLDOWN    = 300            # min seconds between auto-restarts
STATEF      = "/run/goesproc-watchdog.state"
LASTF       = "/run/goesproc-watchdog.last"
# Only dirs fed by the satellite stream. NOT /srv/goes/loop -- its graphics are
# fetched from the web every 2 min and would mask a totally dead downlink.
PRODUCT_DIRS = ["/srv/goes/emwin", "/srv/goes/goes19"]


def ha(path, obj):
    if not (HA_URL and HA_TOKEN): return
    try:
        req = urllib.request.Request(HA_URL + path, data=json.dumps(obj).encode(),
              headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print("HA post error", path, e)


def sh(*args):
    return subprocess.run(args, capture_output=True, text=True).stdout


def upstream():
    """(host, port) goesproc actually subscribes to, read off the unit so this
    never drifts from goesproc.service."""
    m = re.search(r"tcp://([0-9.]+):(\d+)", sh("systemctl", "show", "goesproc", "-p", "ExecStart", "--value"))
    return (m.group(1), m.group(2)) if m else ("10.10.0.9", "5004")


def stream_age_ms(host, port):
    """Milliseconds since the packet socket last received data; None if there is
    no established socket at all (goesproc not connected)."""
    out = sh("ss", "-tniH", "state", "established", "dst", f"{host}:{port}")
    if not out.strip(): return None
    m = re.search(r"lastrcv:(\d+)", out)
    return int(m.group(1)) if m else None


def uptime_s():
    """Seconds since goesproc last entered active.

    Must compare against time.monotonic(), NOT /proc/uptime: in this LXC
    /proc/uptime is lxcfs-virtualised to the *container's* uptime while systemd's
    monotonic timestamps are on the *host* boot clock (observed 44 days apart).
    Mixing them yielded a negative age, which made the GRACE guard below swallow
    every fault and report OK forever."""
    ts = sh("systemctl", "show", "goesproc", "-p", "ActiveEnterTimestampMonotonic", "--value").strip()
    try:
        return time.monotonic() - float(ts) / 1e6
    except ValueError:
        return 1e9


def products_fresh():
    """Cheap existence test -- find quits at the first hit instead of statting
    a day's worth of EMWIN files."""
    dirs = [d for d in PRODUCT_DIRS if os.path.isdir(d)]
    if not dirs: return False
    hit = sh("find", *dirs, "-type", "f", "-newermt", f"-{PRODUCT_AGE} seconds", "-print", "-quit")
    return bool(hit.strip())


def read(f, d=""):
    try: return open(f).read().strip()
    except OSError: return d


def main():
    active = subprocess.run(["systemctl", "is-active", "--quiet", "goesproc"]).returncode == 0
    host, port = upstream()
    age_ms = stream_age_ms(host, port)
    up = uptime_s()

    if not active:
        cur = "down"
    elif up < GRACE:                       # just restarted -- not yet evidence of anything
        cur = "ok"
    elif age_ms is None:
        cur = "disconnected"               # no socket to the Pi at all
    elif age_ms > STALL_MS:
        cur = "stalled"                    # THE half-open case: ESTABLISHED but silent
    elif not products_fresh():
        cur = "no-products"                # stream alive, goesproc not writing
    else:
        cur = "ok"

    ha("/api/states/sensor.goesproc_status", {
        "state": {"ok": "Processing", "stalled": "Stream stalled", "disconnected": "Disconnected",
                  "no-products": "No products", "down": "Down"}.get(cur, cur),
        "attributes": {"friendly_name": "GOES-19 Processing",
            "icon": "mdi:satellite-variant" if cur == "ok" else "mdi:satellite-uplink",
            "upstream": f"{host}:{port}",
            "stream_age_s": None if age_ms is None else round(age_ms / 1000, 1),
            "goesproc_uptime_s": round(up),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S %Z")}})

    if cur != "ok":
        now = time.time(); last = float(read(LASTF, "0") or 0)
        if now - last >= COOLDOWN:
            open(LASTF, "w").write(str(now))
            subprocess.run(["systemctl", "restart", "goesproc"])
            subprocess.run(["logger", "-t", "goesproc-watchdog", f"{cur} -- restarted goesproc"])

    open(STATEF, "w").write(cur)


if __name__ == "__main__":
    main()
