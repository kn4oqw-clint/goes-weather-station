#!/usr/bin/env python3
"""Keep the latest of selected EMWIN graphic products (normalized to PNG for a
stable URL), serve them for Home Assistant, and delete all other unused graphics.
Products NOT carried on the EMWIN broadcast are fetched from public NWS web URLs.
Served by goes-web on :8099 -> http://<lxc>:8099/graphics/<slug>.png"""
import os, re, glob, time, urllib.request
from io import BytesIO
from PIL import Image

EMWIN = "/srv/goes/emwin"
OUT   = "/srv/goes/loop/graphics"     # under the goes-web docroot (/srv/goes/loop)
MAXAGE = 7200                          # delete raw (unused) EMWIN graphics older than 2h; selected ones
                                       # are copied to OUT first so tiles persist between issuances

# EMWIN product id (suffix before extension) -> (slug matching camera.wx_<slug>, friendly name).
SELECTED = {
    "RADSTHES": ("radar",     "Southeast US Radar (NWS)"),
    "CSA001US": ("tropical",  "NHC Tropical Surface Analysis"),
    "G02HURUS": ("surface",   "Tropical Atlantic (GOES-19 Enhanced IR)"),
    "G16CIRUS": ("spc_day1",  "CONUS East IR 2km (GOES-19)"),
    "IMGWWAUS": ("warnings",  "Active Warnings & Watches (SPC)"),
}

UA = {"User-Agent": "Mozilla/5.0 (goes-weather-station)"}

def _get(url, timeout=25):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()

def spc_day1_png_url():
    """SPC removed the static day1otlk.gif; the current categorical map is a
    time-stamped 'print' PNG (day1otlk_<HHMM>_prt.png). Resolve the CURRENT
    issuance from the index page so we always fetch the latest (and so it rolls
    over to the new day). Prefer the _prt link whose time matches the page's
    'Valid DDHHMMZ' stamp — this is robust even if the page ever lists archive
    links alongside the current one (a bare re.search could otherwise stick on
    a stale, earlier issuance across the day boundary)."""
    try:
        html = _get("https://www.spc.noaa.gov/products/outlook/day1otlk.html").decode("latin-1")
        times = re.findall(r"day1otlk_(\d{4})_prt\.html", html)
        if not times:
            return None
        valid = re.search(r"Valid\s+\d{2}(\d{4})Z", html)   # DDHHMM -> HHMM of the current outlook
        hhmm = valid.group(1) if valid and valid.group(1) in times else times[-1]
        return f"https://www.spc.noaa.gov/products/outlook/day1otlk_{hhmm}_prt.png"
    except Exception as e:
        print("SPC resolve failed", e)
    return None

# slug -> callable returning the current image URL (products not on the EMWIN feed)
WEB = {"spc_conv": spc_day1_png_url}

def fetch_web():
    for slug, resolver in WEB.items():
        url = resolver()
        if not url:
            continue
        try:
            dst = os.path.join(OUT, slug + ".png")
            Image.open(BytesIO(_get(url))).convert("RGB").save(dst + ".tmp", "PNG")
            os.replace(dst + ".tmp", dst)
        except Exception as e:
            print("web fetch failed", slug, url, e)

def newest(pid):
    pat = re.compile(rf"-{re.escape(pid)}\.(GIF|JPG|PNG)$", re.I)
    cands = [f for f in glob.glob(os.path.join(EMWIN, "**", "*"), recursive=True)
             if os.path.isfile(f) and pat.search(os.path.basename(f))]
    return max(cands, key=os.path.getmtime) if cands else None

def main():
    os.makedirs(OUT, exist_ok=True)
    fetch_web()
    for pid, (slug, _name) in SELECTED.items():
        src = newest(pid)
        if not src:
            continue
        dst = os.path.join(OUT, slug + ".png")
        try:
            Image.open(src).convert("RGB").save(dst + ".tmp", "PNG")
            os.replace(dst + ".tmp", dst)
        except Exception as e:
            print("convert failed", src, e)
    # cleanup: raw EMWIN graphics are only needed transiently — selected ones are
    # already copied to OUT; delete everything older than MAXAGE.
    now = time.time()
    for f in glob.glob(os.path.join(EMWIN, "**", "*"), recursive=True):
        if os.path.isfile(f) and not f.lower().endswith(".txt"):
            try:
                if now - os.path.getmtime(f) > MAXAGE:
                    os.remove(f)
            except OSError:
                pass

if __name__ == "__main__":
    main()
