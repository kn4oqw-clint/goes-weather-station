#!/usr/bin/env bash
# Deploy goesproc + loop + alerts + uploader on the PVE processing container.
# Run as root inside the LXC after build-goestools.sh. No sudo in minimal LXC.
set -euo pipefail
GOES_S3_SECRET="${GOES_S3_SECRET:?export GOES_S3_SECRET first}"
HA_TOKEN="${HA_TOKEN:?export HA_TOKEN first}"

id goes &>/dev/null || useradd -r -s /usr/sbin/nologin -d /srv/goes goes
install -d -o goes -g goes /srv/goes /var/lib/goes
install -d -m0750 -o root -g goes /etc/goes

# configs + units from this repo dir
install -m0644 etc/goesproc.conf /etc/goesproc.conf
install -m0644 systemd/*.service systemd/*.timer /etc/systemd/system/
install -m0755 make_loop.py emwin_alerts.py goesproc-watchdog.py seed_map_assets.py /opt/

# --- offline Leaflet map served by goes-web on :8099 ------------------------
# libjs-leaflet keeps the map self-hosted (no CDN); pyshp reads the NWS
# county/zone shapefiles for the UGC lookup. Both from apt, so an air-gapped
# rebuild only needs the local mirror.
apt-get install -y -qq libjs-leaflet python3-pyshp
install -d -o goes -g goes /srv/goes/loop/vendor/leaflet
cp /usr/share/javascript/leaflet/leaflet.js /usr/share/javascript/leaflet/leaflet.css \
   /srv/goes/loop/vendor/leaflet/
cp -r /usr/share/javascript/leaflet/images /srv/goes/loop/vendor/leaflet/
install -o goes -g goes -m0644 map.html /srv/goes/loop/map.html
# Prefix symlink so the map also answers under /goesmap/... NPMplus fronts HA at
# https://home.thechance.family and an http iframe there is blocked as mixed
# content, so the map is exposed as a /goesmap location on that same host. That
# location forwards the prefix intact (its generated proxy_pass interpolates
# $request_uri, which makes any nginx rewrite a no-op), so the backend has to
# answer /goesmap/* itself. Existing URLs like /loop.gif are unaffected.
ln -sfn /srv/goes/loop /srv/goes/loop/goesmap
chown -h goes:goes /srv/goes/loop/goesmap
chown -R goes:goes /srv/goes/loop/vendor
# Basemap always; UGC shapes need one download and are optional -- storm-based
# warning polygons arrive over the dish and render without them.
runuser -u goes -- python3 /opt/seed_map_assets.py || \
  echo "WARN: UGC seeding failed; watches/advisories will have no shape on the map"

# secrets (from env, never committed)
sed "s#REPLACE_WITH_GOES_S3_SECRET#${GOES_S3_SECRET}#" etc/goes/rclone.conf.example > /etc/goes/rclone.conf
sed "s#REPLACE_WITH_HA_LONG_LIVED_TOKEN#${HA_TOKEN}#" etc/goes/ha.env.example > /etc/goes/ha.env
chown root:goes /etc/goes/rclone.conf /etc/goes/ha.env
chmod 0640 /etc/goes/rclone.conf /etc/goes/ha.env

systemctl daemon-reload
systemctl enable --now goesproc goes-upload.timer goes-prune.timer goes-loop.timer goes-web goes-alerts \
  goesproc-watchdog.timer
echo "deployed. goesproc subscribes to the Pi at tcp://10.10.0.9:5004"
