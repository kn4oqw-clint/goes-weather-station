#!/usr/bin/env python3
"""Watch decoded EMWIN/NWS text for the local WFO (KMOB), parse VTEC + UGC zones,
track active hazards, and publish to Home Assistant. HA OWNS notifications: this
daemon only updates sensor.goes_wx_kmob and fires a `goes_wx_warning` event
(carrying the affected UGC codes) so an HA automation can geo-target the push."""
import os,re,glob,json,time,datetime,urllib.request,zoneinfo

HA_URL=os.environ.get("HA_URL","http://10.10.0.5:8123")
HA_TOKEN=os.environ["HA_TOKEN"]
OFFICE="KMOB"                       # local NWS office (WFO Mobile)
WATCH=["/srv/goes/text/nws","/srv/goes/text/other","/srv/goes/emwin"]
STATE="/var/lib/goes/alert_state.json"
ALERTS_OUT="/srv/goes/loop/alerts.json"   # consumed by map.html (served by goes-web)
TZ=zoneinfo.ZoneInfo("America/Chicago")
# Fire a mobile-push event for every new WARNING (.W). Also keep the two
# life-safety WATCHES that used to notify so we don't regress. HA then filters
# by UGC (home zone + live device location) before actually pushing.
PUSH_WATCH={("TO","A"),("SV","A")}
def should_push(phen,sig): return sig=="W" or (phen,sig) in PUSH_WATCH
PHEN={"TO":"Tornado","SV":"Severe Thunderstorm","FF":"Flash Flood","FL":"Flood","FA":"Areal Flood",
      "MA":"Marine","SM":"Special Marine","EW":"Extreme Wind","HU":"Hurricane","TR":"Tropical Storm",
      "HW":"High Wind","WI":"Wind","HT":"Heat","FG":"Dense Fog","CF":"Coastal Flood","RP":"Rip Current",
      "SU":"High Surf","FZ":"Freeze","WS":"Winter Storm","BZ":"Blizzard","FR":"Frost","SV.A":"Svr Watch"}
SIG={"W":"Warning","A":"Watch","Y":"Advisory","S":"Statement","F":"Forecast","O":"Outlook"}
VTEC=re.compile(r"/[OTEX]\.(NEW|CON|CAN|EXP|EXA|EXB|EXT|UPG|ROU)\.([A-Z]{4})\.([A-Z]{2})\.([AWYSFO])\.(\d{4})\.(\d{6}T\d{4}Z)-(\d{6}T\d{4}Z)/")

# --- UGC (Universal Geographic Code) parsing --------------------------------
# Each VTEC segment is preceded by a UGC block naming the affected counties (C)
# or forecast zones (Z), e.g. "FLZ201>206-ALZ051-058-072000-" ending in a
# DDHHMM purge time. Codes wrap across lines and use ">" ranges + state/type
# continuation (a bare 3-digit token inherits the previous state+type).
UGC_TERM=re.compile(r"(\d{6})-\r?\n")   # the purge-time terminator that ends a UGC block
UGC_GRAMMAR=re.compile(r"((?:[A-Z]{2}[CZ]\d{3}|\d{3})(?:>\d{3})?(?:-(?:[A-Z]{2}[CZ]\d{3}|\d{3})(?:>\d{3})?)*)-$")

def expand_ugc(tokstr):
    out=set(); state=None; typ=None
    for tok in tokstr.split("-"):
        m=re.match(r"^([A-Z]{2})([CZ])(\d{3})(?:>(\d{3}))?$",tok)
        if m:
            state,typ=m.group(1),m.group(2)
            a=int(m.group(3)); b=int(m.group(4) or m.group(3))
            for n in range(a,b+1): out.add(f"{state}{typ}{n:03d}")
            continue
        m=re.match(r"^(\d{3})(?:>(\d{3}))?$",tok)   # continuation: inherit state+type
        if m and state and typ:
            a=int(m.group(1)); b=int(m.group(2) or m.group(1))
            for n in range(a,b+1): out.add(f"{state}{typ}{n:03d}")
    return out

def ugc_blocks(txt):
    """List of (position, set-of-UGC) for every UGC block, sorted by position."""
    res=[]
    for m in UGC_TERM.finditer(txt):
        window=re.sub(r"\s+","",txt[max(0,m.start()-600):m.start()])   # strip line wraps
        g=UGC_GRAMMAR.search(window)
        if g:
            s=expand_ugc(g.group(1))
            if s: res.append((m.start(),s))
    return res

# --- storm-based polygons ---------------------------------------------------
# Warning products carry their own geometry in the text, e.g.
#   LAT...LON 3474 10253 3431 10253 3431 10304 3475 10304
#   TIME...MOT...LOC 0041Z 287DEG 9KT 3465 10284
# Values are hundredths of a degree, longitude POSITIVE WEST. This is the only
# alert geometry that needs no external data at all -- it arrives over the dish,
# so the map keeps drawing warnings even with the internet down. Watches and
# zone advisories have no polygon and fall back to the seeded UGC shapes.
LATLON=re.compile(r"LAT\.\.\.LON((?:\s+\d{3,5})+)")
MOTION=re.compile(r"TIME\.\.\.MOT\.\.\.LOC\s+(\d{4})Z\s+(\d{1,3})DEG\s+(\d{1,3})KT((?:\s+\d{3,5})+)")

def _pairs(tokstr):
    n=[int(t) for t in tokstr.split()]
    return [[-n[i+1]/100.0, n[i]/100.0] for i in range(0,len(n)-1,2)]   # [lon,lat]

def poly_blocks(txt):
    """[(position, polygon, motion|None)] for every LAT...LON block."""
    res=[]
    for m in LATLON.finditer(txt):
        poly=_pairs(m.group(1))
        if len(poly)<3: continue
        mot=None
        mm=MOTION.search(txt,m.end(),m.end()+400)      # same segment as the polygon
        if mm:
            pts=_pairs(mm.group(4))
            # NWS convention: DEG is the direction the storm is coming FROM, so
            # the travel heading is deg+180.
            mot={"deg":int(mm.group(2)),"kt":int(mm.group(3)),
                 "heading":(int(mm.group(2))+180)%360,
                 "loc":pts[0] if pts else None}
        res.append((m.start(),poly,mot))
    return res

def poly_for(pos,blocks):
    """Geometry of the first LAT...LON block AFTER this VTEC segment starts."""
    for bp,poly,mot in blocks:
        if bp>pos: return poly,mot
    return None,None

def ugc_for(pos,blocks):
    """UGC set of the nearest block preceding a VTEC match at `pos`."""
    best=None
    for bp,s in blocks:
        if bp<pos: best=s
        else: break
    return sorted(best) if best else []

def hapost(path,obj):
    req=urllib.request.Request(HA_URL+path,data=json.dumps(obj).encode(),
        headers={"Authorization":f"Bearer {HA_TOKEN}","Content-Type":"application/json"},method="POST")
    try: urllib.request.urlopen(req,timeout=10).read()
    except Exception as e: print("HA post err",path,e)

def vtec_time(s): return datetime.datetime.strptime(s,"%y%m%dT%H%MZ").replace(tzinfo=datetime.timezone.utc)

def load():
    try: return json.load(open(STATE))
    except Exception: return {"seen":[],"active":{}}
def save(st): os.makedirs(os.path.dirname(STATE),exist_ok=True); json.dump(st,open(STATE,"w"))

def push_ha(active):
    now=datetime.datetime.now(datetime.timezone.utc)
    items=[]
    for k,a in active.items():
        exp=vtec_time(a["end"])
        items.append({"event":a["name"],"expires":exp.astimezone(TZ).strftime("%a %I:%M %p %Z"),
                      "etn":a["etn"],"ugc":a.get("ugc",[]),"raw":k})
    items.sort(key=lambda x:x["raw"])
    state=len(items)
    hazards=", ".join(sorted({i["event"] for i in items})) or "None"
    hapost("/api/states/sensor.goes_wx_kmob",{"state":state,
        "attributes":{"friendly_name":"GOES EMWIN Alerts (KMOB)","unit_of_measurement":"active",
            "icon":"mdi:alert" if state else "mdi:shield-check","hazards":hazards,"alerts":items,
            "updated":now.astimezone(TZ).strftime("%Y-%m-%d %I:%M %p %Z")}})

def write_alerts(active):
    """Small JSON for map.html. Only the storm polygon is inlined; zone-based
    alerts ship their UGC list and the page joins against the seeded ugc.json,
    so a 20-county advisory costs a few dozen bytes here instead of megabytes."""
    now=datetime.datetime.now(datetime.timezone.utc)
    out=[]
    for k,a in sorted(active.items()):
        exp=vtec_time(a["end"])
        out.append({"key":k,"name":a["name"],"phen":a["phen"],"sig":a["sig"],"etn":a["etn"],
                    "expires":exp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "expires_local":exp.astimezone(TZ).strftime("%a %I:%M %p %Z"),
                    "ugc":a.get("ugc",[]),"poly":a.get("poly"),"motion":a.get("motion")})
    doc={"updated":now.strftime("%Y-%m-%dT%H:%M:%SZ"),"office":OFFICE,"alerts":out}
    try:
        os.makedirs(os.path.dirname(ALERTS_OUT),exist_ok=True)
        tmp=ALERTS_OUT+".tmp"
        with open(tmp,"w") as fh: json.dump(doc,fh,separators=(",",":"))
        os.replace(tmp,ALERTS_OUT)                     # atomic; the page polls this
    except OSError as e:
        print("alerts.json write err",e)

def main():
    st=load(); seen=set(st.get("seen",[])); active=st.get("active",{})
    push_ha(active); write_alerts(active)
    while True:
        files=[]
        for d in WATCH: files+=glob.glob(os.path.join(d,"**","*.TXT"),recursive=True)+glob.glob(os.path.join(d,"**","*.txt"),recursive=True)
        for f in files:
            b=os.path.basename(f)
            try:
                if (time.time()-os.path.getmtime(f)) < 20: continue   # too fresh; let goesproc finish writing
            except OSError:
                continue
            if b in seen:                                             # already processed a prior cycle
                try: os.remove(f)
                except OSError: pass
                continue
            seen.add(b)
            try: txt=open(f,errors="ignore").read()
            except Exception: txt=""
            blocks=ugc_blocks(txt); pblocks=poly_blocks(txt)
            for m in VTEC.finditer(txt):
                action,office,phen,sig,etn,t0,t1=m.groups()
                if office!=OFFICE: continue          # my WFO only
                key=f"{office}.{phen}.{sig}.{etn}"
                name=f"{PHEN.get(phen,phen)} {SIG.get(sig,sig)}"
                ug=ugc_for(m.start(),blocks)
                poly,mot=poly_for(m.start(),pblocks)
                if action in ("CAN","EXP","UPG"):
                    active.pop(key,None)
                else:
                    isnew = key not in active and action=="NEW"
                    prev=active.get(key) or {}
                    # A follow-up statement (SVS) may omit the polygon; keep the
                    # last known one rather than dropping the shape off the map.
                    active[key]={"name":name,"end":t1,"etn":etn,"phen":phen,"sig":sig,"ugc":ug,
                                 "poly":poly or prev.get("poly"),"motion":mot or prev.get("motion")}
                    if isnew and should_push(phen,sig):
                        # HA owns the notification; fire an event carrying the
                        # affected UGC zones so the automation can geo-target.
                        exp_local=vtec_time(t1).astimezone(TZ).strftime("%a %I:%M %p %Z")
                        hapost("/api/events/goes_wx_warning",
                            {"event":name,"phen":phen,"sig":sig,"etn":etn,"office":office,
                             "expires":t1,"expires_local":exp_local,"ugc":ug,
                             "message":f"NWS Mobile issued a {name} (#{etn}). In effect until {exp_local}. Source: GOES-19 EMWIN."})
            # NWS text is consumed only here (alerts) — delete after processing; graphics handled separately
            try: os.remove(f)
            except OSError: pass
        # expire old
        now=datetime.datetime.now(datetime.timezone.utc)
        for k in [k for k,a in active.items() if vtec_time(a["end"])<now]: active.pop(k,None)
        push_ha(active); write_alerts(active)
        st={"seen":list(seen)[-5000:],"active":active}; save(st)
        time.sleep(30)

if __name__=="__main__": main()
