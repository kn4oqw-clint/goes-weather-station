#!/usr/bin/env python3
"""Add a /goesmap custom location to the existing home.thechance.family proxy
host in NPMplus, so the Leaflet map is reachable over the SAME https origin as
Home Assistant (an http iframe on an https page is blocked as mixed content).

  npm_goesmap.py inspect     show current host + locations (read-only)
  npm_goesmap.py apply       add/update the location (idempotent), backs up first

Credentials from NPM_IDENTITY / NPM_SECRET env vars.
"""
import http.cookiejar, json, os, ssl, sys, urllib.request, time

BASE = "https://172.16.50.136:81/api"
HOSTNAME = "home.thechance.family"
PATH = "/goesmap"
BACKEND = ("http", "10.10.0.11", 8099)
# NPMplus generates `proxy_pass http://upstream_N_location_0$request_uri;`. Because
# that proxy_pass contains a VARIABLE, nginx passes $request_uri verbatim and
# ignores any rewrite in the location -- so the usual `rewrite ^/goesmap/?(.*)$
# /$1 break;` trick silently does nothing and the backend 404s on /goesmap/...
# The prefix is therefore handled on the backend instead, by a symlink
# /srv/goes/loop/goesmap -> /srv/goes/loop (see deploy.sh). Nothing to add here.
ADVANCED = ""

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE          # NPM admin UI uses a self-signed cert
# NPMplus does NOT return the JWT in the body like upstream NPM -- POST /tokens
# answers {"expires": ...} and sets a __Host-Http-token HttpOnly cookie. So carry
# a cookie jar; an Authorization: Bearer header alone gets you 403.
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=CTX),
                                     urllib.request.HTTPCookieProcessor(JAR))


def call(method, path, token=None, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    data = json.dumps(body).encode() if body is not None else None
    with OPENER.open(req, data, timeout=30) as r:
        return json.loads(r.read() or "null")


def login():
    call("POST", "/tokens", body={"identity": os.environ["NPM_IDENTITY"],
                                  "secret": os.environ["NPM_SECRET"]})
    return None                      # auth now lives in the cookie jar


def find_host(token):
    # NOTE: upstream NPM's ?expand=owner,access_list,certificate returns 500 on
    # this fork. Plain list to find the id, then fetch the single host.
    for h in call("GET", "/nginx/proxy-hosts", token):
        if HOSTNAME in (h.get("domain_names") or []):
            return call("GET", "/nginx/proxy-hosts/%s" % h["id"], token)
    raise SystemExit("proxy host for %s not found" % HOSTNAME)


if __name__ == "__main__":
    act = sys.argv[1] if len(sys.argv) > 1 else "inspect"
    tok = login()
    h = find_host(tok)
    print("host id=%s domains=%s -> %s://%s:%s ssl_forced=%s http2=%s"
          % (h["id"], h["domain_names"], h["forward_scheme"], h["forward_host"],
             h["forward_port"], h.get("ssl_forced"), h.get("http2_support")))
    locs = h.get("locations") or []
    print("existing locations: %d" % len(locs))
    for l in locs:
        print("   %s -> %s://%s:%s  adv=%r"
              % (l.get("path"), l.get("forward_scheme"), l.get("forward_host"),
                 l.get("forward_port"), (l.get("advanced_config") or "")[:60]))

    if act != "apply":
        sys.exit(0)

    bak = "/tmp/npm_proxyhost_%s_%s.bak.json" % (h["id"], time.strftime("%Y%m%d-%H%M%S"))
    json.dump(h, open(bak, "w"), indent=1)
    print("backup ->", bak)

    # This fork's location schema REQUIRES npmplus_access_list_* on every entry;
    # omitting them fails validation with a 400. Mirror the parent host ("public",
    # no ACL) so the map inherits exactly the host's access posture.
    new = {"path": PATH, "advanced_config": ADVANCED,
           "forward_scheme": BACKEND[0], "forward_host": BACKEND[1], "forward_port": BACKEND[2],
           "npmplus_access_list_ids": h.get("npmplus_access_list_ids", []),
           "npmplus_access_list_type": h.get("npmplus_access_list_type", "public")}
    locs = [l for l in locs if l.get("path") != PATH] + [new]     # idempotent

    # Send back the mutable fields NPM expects; anything omitted can be reset.
    payload = {"locations": locs}      # PUT is a partial update; touch nothing else
    call("PUT", "/nginx/proxy-hosts/%s" % h["id"], tok, payload)
    print("applied: %s -> %s://%s:%s" % (PATH, *BACKEND))
