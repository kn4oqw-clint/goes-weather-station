#!/usr/bin/env python3
"""Crop recent GOES-19 FD Band-13 frames to the KMOB region (reprojected to
Plate Carree), colour-enhance them on the physical temperature scale, draw the
map overlay, and assemble an animated loop for Home Assistant."""
import glob, os, re, sys, json, subprocess, datetime, zoneinfo
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Overlay-free LOSSLESS source + its calibration sidecar (goesproc "raw" handler).
# NOT /srv/goes/goes19/fd/ch13 -- that is the display JPEG with coastlines baked
# into the pixels at value 255, which the calibration table reads as 89.6 K, i.e.
# colder than any real cloud. Rendering that put ~6100 bogus "colder than deep
# convection" pixels along every coastline, and JPEG ringing pushed the crop's
# 99th percentile from 135 to 254. Map lines are now drawn as vectors below.
SRC="/srv/goes/raw/goes19/fd/ch13"
NE_DIR="/usr/local/share/goestools/ne"
OUT="/srv/goes/loop"; FRAMES=os.path.join(OUT,"frames")
MAPDIR=os.path.join(OUT,"map")        # bare Web Mercator frames for the Leaflet map
# GOES-19 HRIT full disk arrives every 30 MIN (verified 00:00:21Z / 00:30:21Z,
# every channel) -- not the 10-15 min this once assumed. 18 frames is therefore a
# ~9 h loop, not the "3h loop" the HA card is titled. Drop to 6 for a true 3 h.
NFRAMES=18
OW,OH=900,777
LON_MIN,LON_MAX,LAT_MIN,LAT_MAX=-95.4,-78.6,23.4,37.9   # ~500 mi around the house

# The Leaflet map gets its OWN, much wider extent: the regional box above is a
# fine still image but on a pannable/zoomable map you immediately hit its edge
# and the rest of the country is empty. HRIT carries no CONUS sector (only fd,
# m1, m2), so full-country coverage means cropping the full disk wider.
# 1800px over 59 deg is ~3.0 km/px at 30N, which matches what ABI actually
# resolves over CONUS -- going wider in pixels would add file size, not detail.
MAP_LON_MIN,MAP_LON_MAX,MAP_LAT_MIN,MAP_LAT_MAX=-125.5,-66.5,23.5,50.0
MAP_OW=1800
TZ=zoneinfo.ZoneInfo("America/Chicago")
MAP_RGB=(255,255,255); MAP_STATE_RGB=(170,170,170)

# Station marker. Every hue is already spoken for by the enhancement ramp, so the
# marker is made unambiguous by SHAPE (ring + centre dot) and by a black halo
# rather than by colour -- it stays legible over grey, over a red core, and over
# the white map lines. Set HOME_LABEL="" to drop the text and keep just the ring.
HOME_LAT,HOME_LON=30.6435,-87.0545      # KN4OQW-13 / WFO KMOB, matches setup.env
HOME_LABEL=""; HOME_RGB=(255,255,255); HOME_HALO=(0,0,0); HOME_R=4

# GOES-R fixed grid (FD 2km) projection constants
LON0=np.deg2rad(-75.0); H=42164160.0; R_EQ=6378137.0; R_POL=6356752.31414
E2=(R_EQ**2-R_POL**2)/R_EQ**2
XOFF=-0.151844; XSCALE=5.6e-05; YOFF=0.151844; YSCALE=-5.6e-05

def _merc(deg): return np.log(np.tan(np.pi/4+np.deg2rad(deg)/2))

def mercator_height(ow,lon_min,lon_max,lat_min,lat_max):
    """Rows needed for square pixels in Web Mercator at this aspect."""
    dy=_merc(lat_max)-_merc(lat_min); dx=np.deg2rad(lon_max-lon_min)
    return int(round(ow*dy/dx))

MAP_OH=mercator_height(MAP_OW,MAP_LON_MIN,MAP_LON_MAX,MAP_LAT_MIN,MAP_LAT_MAX)

def lat_axis(mercator,lat_min,lat_max,oh):
    """Row -> latitude. Plate Carree is linear in latitude; the Leaflet overlay
    instead needs rows linear in Web Mercator y so L.imageOverlay lines up under
    the default EPSG3857 CRS (placing an equirectangular image there would smear
    it north-south by ~1/cos(lat) -- 31 km at this crop's edges)."""
    if not mercator: return np.linspace(lat_max,lat_min,oh)
    return np.rad2deg(2*np.arctan(np.exp(np.linspace(_merc(lat_max),_merc(lat_min),oh)))-np.pi/2)

def build_map(mercator=False,box=None,ow=None,oh=None):
    lo_min,lo_max,la_min,la_max = box or (LON_MIN,LON_MAX,LAT_MIN,LAT_MAX)
    ow=ow or OW; oh=oh or OH
    lons=np.linspace(lo_min,lo_max,ow); lats=lat_axis(mercator,la_min,la_max,oh)
    LON,LAT=np.meshgrid(np.deg2rad(lons),np.deg2rad(lats))
    phi_c=np.arctan((R_POL**2/R_EQ**2)*np.tan(LAT))
    rc=R_POL/np.sqrt(1-E2*np.cos(phi_c)**2)
    sx=H-rc*np.cos(phi_c)*np.cos(LON-LON0); sy=-rc*np.cos(phi_c)*np.sin(LON-LON0); sz=rc*np.sin(phi_c)
    x=np.arcsin(-sy/np.sqrt(sx**2+sy**2+sz**2)); y=np.arctan2(sz,sx)
    vis=H*(H-sx) >= (sy**2+(R_EQ**2/R_POL**2)*sz**2)
    col=np.clip(np.round((x-XOFF)/XSCALE).astype(int),0,5423)
    row=np.clip(np.round((y-YOFF)/YSCALE).astype(int),0,5423)
    return col,row,vis
COL,ROW,VIS=build_map(False)          # HA camera card: regional, annotated
MCOL,MROW,MVIS=build_map(True,(MAP_LON_MIN,MAP_LON_MAX,MAP_LAT_MIN,MAP_LAT_MAX),
                         MAP_OW,MAP_OH)   # Leaflet overlay: CONUS, bare, Mercator

# ---------------------------------------------------------------------------
# Enhancement, defined on the PHYSICAL scale (deg C of cloud-top temperature).
#
# It must be built from the HRIT calibration table, not from raw 8-bit counts.
# That table is linear but inverted and spans 340.35 K (value 0) down to 89.6 K
# (value 255) at ~0.983 K/count. Spreading colour anchors evenly over the count
# range -- the previous approach -- therefore placed yellow at 180 K, orange at
# 155 K, red at 132 K and magenta at 112 K. Nothing on Earth is that cold: the
# coldest pixel in this crop is ~197 K (-76 C) and global overshooting tops stop
# near 180 K, so every warm colour was unreachable and the loop rendered nothing
# but blue and green while NOAA's product showed the full ramp on the same storm.
#
# Tune the look here. Colour is spent entirely on -30..-90 C, where convection
# actually lives; warmer than -30 C stays greyscale like the NESDIS product.
GREY_WARM_C, GREY_COLD_C = 40.0, -30.0          # greyscale span (warm -> white)
GREY_LO, GREY_HI = 40, 255                      # grey level at each end
COLOUR_ANCHORS=[(-30,(  0,  0,120)),            # deep blue
                (-40,(  0,120,235)),            # blue
                (-45,(  0,210,220)),            # cyan
                (-50,(  0,190,  0)),            # green
                (-58,(215,215,  0)),            # yellow
                (-64,(255,150,  0)),            # orange
                (-70,(225,  0,  0)),            # red
                (-76,(140,  0,  0)),            # dark red
                (-80,(255,  0,255)),            # magenta
                (-90,(255,255,255))]            # white

# Fallback only: the calibration observed on every G19 CH13 frame to date. Used
# if a frame's .json sidecar is missing so a bad/absent file cannot silently
# reintroduce a wrong scale.
_FALLBACK_TABLE=np.linspace(340.3494873046875,89.62000274658203,256)

def build_lut(table_k):
    """256-entry RGB LUT keyed by raw pixel value, via the frame's own K table."""
    lut=np.zeros((256,3),dtype=np.uint8)
    Tc=np.asarray(table_k,dtype=float)-273.15
    warm=Tc>=GREY_COLD_C
    if warm.any():
        f=np.clip((GREY_WARM_C-Tc[warm])/(GREY_WARM_C-GREY_COLD_C),0,1)
        g=(f*(GREY_HI-GREY_LO)+GREY_LO).astype(np.uint8)
        lut[warm]=np.stack([g,g,g],axis=1)
    if (~warm).any():
        xs=[a[0] for a in COLOUR_ANCHORS][::-1]          # ascending for np.interp
        cold=np.clip(Tc[~warm],COLOUR_ANCHORS[-1][0],COLOUR_ANCHORS[0][0])
        for ch in range(3):
            ys=[a[1][ch] for a in COLOUR_ANCHORS][::-1]
            lut[~warm,ch]=np.interp(cold,xs,ys).astype(np.uint8)
    return lut

_lut_cache={}
def lut_for(table_k):
    key=(round(float(table_k[0]),3),round(float(table_k[-1]),3))
    if key not in _lut_cache: _lut_cache[key]=build_lut(table_k)
    return _lut_cache[key]

def calibration(png_path):
    """Brightness-temperature table (K) for this frame, from its goesproc sidecar."""
    try:
        meta=json.load(open(os.path.splitext(png_path)[0]+".json"))
        tbl=np.asarray(meta["ImageDataFunction"]["Table"],dtype=float)
        if tbl.size==256 and np.all(np.diff(tbl)<0): return tbl
        print(f"warn: unusable calibration table in {png_path}, using fallback")
    except (OSError, ValueError, KeyError) as e:
        print(f"warn: no calibration sidecar for {os.path.basename(png_path)} ({e}); using fallback")
    return _FALLBACK_TABLE

# ---------------------------------------------------------------------------
# Map overlay. The output grid is Plate Carree, so lon/lat -> pixel is linear.
def _rings(path):
    out=[]
    try: gj=json.load(open(path))
    except (OSError, ValueError) as e:
        print(f"warn: no map overlay from {path} ({e})"); return out
    for feat in gj.get("features",[]):
        geom=feat.get("geometry") or {}
        polys=geom.get("coordinates") or []
        if geom.get("type")=="Polygon": polys=[polys]
        elif geom.get("type")!="MultiPolygon": continue
        for poly in polys:
            for ring in poly:
                a=np.asarray(ring,dtype=float)
                if a.ndim!=2 or len(a)<2: continue
                lon,lat=a[:,0],a[:,1]
                if lon.max()<LON_MIN or lon.min()>LON_MAX or lat.max()<LAT_MIN or lat.min()>LAT_MAX:
                    continue                                   # outside the crop
                xs=(lon-LON_MIN)/(LON_MAX-LON_MIN)*(OW-1)
                ys=(LAT_MAX-lat)/(LAT_MAX-LAT_MIN)*(OH-1)
                out.append(list(zip(xs.tolist(),ys.tolist())))
    return out

_overlay=None
def overlay():
    """(country_rings, state_rings), parsed once per process and reused."""
    global _overlay
    if _overlay is None:
        _overlay=(_rings(os.path.join(NE_DIR,"ne_50m_admin_0_countries_lakes.json")),
                  _rings(os.path.join(NE_DIR,"ne_50m_admin_1_states_provinces_lakes.json")))
    return _overlay

def draw_map(d):
    countries,states=overlay()
    for ring in states:    d.line(ring,fill=MAP_STATE_RGB,width=1)
    for ring in countries: d.line(ring,fill=MAP_RGB,width=1)

def lonlat_to_px(lon,lat):
    """Plate Carree output grid -> pixel. Linear by construction."""
    return ((lon-LON_MIN)/(LON_MAX-LON_MIN)*(OW-1), (LAT_MAX-lat)/(LAT_MAX-LAT_MIN)*(OH-1))

def draw_home(d,font=None):
    if not (LON_MIN<=HOME_LON<=LON_MAX and LAT_MIN<=HOME_LAT<=LAT_MAX):
        return                                             # station outside the crop
    x,y=lonlat_to_px(HOME_LON,HOME_LAT); r=HOME_R
    d.ellipse([x-r-1,y-r-1,x+r+1,y+r+1],outline=HOME_HALO,width=2)   # halo first
    d.ellipse([x-r,y-r,x+r,y+r],outline=HOME_RGB,width=1)
    d.ellipse([x-1,y-1,x+1,y+1],fill=HOME_RGB)
    if HOME_LABEL and font:
        tx,ty=x+r+5,y-9
        for dx,dy in ((-1,0),(1,0),(0,-1),(0,1),(-1,-1),(1,-1),(-1,1),(1,1)):
            d.text((tx+dx,ty+dy),HOME_LABEL,fill=HOME_HALO,font=font)
        d.text((tx,ty),HOME_LABEL,fill=HOME_RGB,font=font)

def ts_from_name(fn):
    m=re.search(r'(\d{8}T\d{6}Z)',fn)
    if not m: return None
    return datetime.datetime.strptime(m.group(1),"%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)

def render(path):
    im=np.asarray(Image.open(path).convert("L"))
    if im.shape!=(5424,5424): return None
    out=im[ROW,COL]; out[~VIS]=0
    img=Image.fromarray(lut_for(calibration(path))[out],"RGB")
    t=ts_from_name(os.path.basename(path))
    d=ImageDraw.Draw(img)
    try:
        font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",22)
        small=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",15)
    except Exception: font=small=ImageFont.load_default()
    draw_map(d)
    draw_home(d,small)
    if t:
        loc=t.astimezone(TZ)
        label=f"GOES-19  Band 13 (Enhanced IR)   {t:%Y-%m-%d %H:%M} UTC  /  {loc:%I:%M %p %Z}"
    else: label="GOES-19 Band 13 (Enhanced IR)"
    d.rectangle([0,OH-34,OW,OH],fill=(0,0,0)); d.text((8,OH-30),label,fill=(255,255,255),font=font)
    return img

def render_map(path):
    """Bare Web Mercator RGBA frame for L.imageOverlay: no burned-in coastlines,
    no marker, no caption -- Leaflet draws all of that as real layers. Off-disk
    pixels are transparent so the basemap shows through instead of a black bar."""
    im=np.asarray(Image.open(path).convert("L"))
    if im.shape!=(5424,5424): return None
    vals=im[MROW,MCOL]
    rgb=lut_for(calibration(path))[vals]
    a=np.where(MVIS,255,0).astype(np.uint8)
    return Image.fromarray(np.dstack([rgb,a]),"RGBA")

def write_map_frames(files):
    """Mercator frames + manifest consumed by map.html."""
    os.makedirs(MAPDIR,exist_ok=True)
    for f in glob.glob(os.path.join(MAPDIR,"*.png")): os.remove(f)
    entries=[]
    for i,f in enumerate(files):
        img=render_map(f)
        if img is None: continue
        # Name by TIMESTAMP, not index. Indexed names get reused every rebuild
        # with different content (frame_000 is a new image each time the window
        # slides), so a browser would happily serve a cached frame_000.png and
        # show imagery from hours ago. Timestamped names are content-stable:
        # same URL always means the same picture, so caching is both safe and
        # useful, and a frame that is still in the window is not refetched.
        t=ts_from_name(os.path.basename(f))
        name=(f"ir_{t:%Y%m%dT%H%M%SZ}.png" if t else f"frame_{i:03d}.png")
        # Palettise: the browser refetches the whole set every rebuild, and the
        # enhancement only spans ~150 distinct colours anyway. FASTOCTREE is the
        # one PIL method that keeps the alpha channel.
        img.quantize(colors=128,method=Image.FASTOCTREE).save(os.path.join(MAPDIR,name),optimize=True)
        entries.append({"file":f"map/{name}",
                        "utc":t.strftime("%Y-%m-%dT%H:%M:%SZ") if t else None,
                        "local":t.astimezone(TZ).strftime("%I:%M %p %Z") if t else None})
    # Ship the palette so map.html renders its legend from the SAME anchors the
    # frames were coloured with -- a hand-copied legend in JS would silently lie
    # the first time COLOUR_ANCHORS is tuned.
    manifest={"bounds":[[MAP_LAT_MIN,MAP_LON_MIN],[MAP_LAT_MAX,MAP_LON_MAX]],  # Leaflet [[S,W],[N,E]]
              # Imagery spans CONUS, but open zoomed to the local area -- fitting
              # the whole country on load would put home in a handful of pixels.
              "home_view":[[LAT_MIN,LON_MIN],[LAT_MAX,LON_MAX]],
              "home":[HOME_LAT,HOME_LON],"crs":"EPSG3857",
              "palette":{"grey":[GREY_WARM_C,GREY_COLD_C,GREY_LO,GREY_HI],
                         "colour":[[t,list(rgb)] for t,rgb in COLOUR_ANCHORS]},
              "frames":entries}
    tmp=os.path.join(OUT,"frames.json.tmp")
    with open(tmp,"w") as fh: json.dump(manifest,fh)
    os.replace(tmp,os.path.join(OUT,"frames.json"))            # atomic for pollers
    return len(entries)

def main():
    os.makedirs(FRAMES,exist_ok=True)
    files=sorted(glob.glob(os.path.join(SRC,"**","*.png"),recursive=True), key=lambda p: ts_from_name(os.path.basename(p)) or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc))
    files=files[-NFRAMES:]
    if not files: print("no FD ch13 frames yet"); return
    for f in glob.glob(os.path.join(FRAMES,"*.png")): os.remove(f)
    imgs=[]
    for i,f in enumerate(files):
        img=render(f)
        if img is None: continue
        img.save(os.path.join(FRAMES,f"frame_{i:03d}.png")); imgs.append(img)
    if not imgs: print("no valid frames"); return
    nmap=write_map_frames(files)
    imgs[-1].save(os.path.join(OUT,"latest.jpg"),quality=88)
    # animated GIF (hold last frame longer)
    durs=[400]*(len(imgs)-1)+[1500]
    pal=[im.convert("P",palette=Image.ADAPTIVE,colors=96) for im in imgs]
    pal[0].save(os.path.join(OUT,"loop.gif"),save_all=True,append_images=pal[1:],duration=durs,loop=0,optimize=True,disposal=2)
    # MP4 via ffmpeg
    subprocess.run(["ffmpeg","-y","-framerate","4","-i",os.path.join(FRAMES,"frame_%03d.png"),
                    "-vf","pad=ceil(iw/2)*2:ceil(ih/2)*2","-pix_fmt","yuv420p","-loglevel","error",
                    os.path.join(OUT,"loop.mp4")],check=False)
    print(f"loop built: {len(imgs)} frames -> {OUT}/loop.gif, loop.mp4, latest.jpg; {nmap} map frames -> {OUT}/frames.json")

if __name__=="__main__": main()
