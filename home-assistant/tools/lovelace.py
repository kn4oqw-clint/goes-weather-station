#!/usr/bin/env python3
"""Read / write a Home Assistant Lovelace dashboard over the WebSocket API.

Storage-mode dashboards are not reachable through the REST API and must never be
edited by hand in `.storage` -- HA caches config in memory and would overwrite
the file. `lovelace/config` + `lovelace/config/save` is the supported path.

  python lovelace.py get  <url_path>            dump current config as JSON
  python lovelace.py save <url_path> <file>     replace config from a JSON file

Always `get` first and keep the output: `save` replaces the ENTIRE dashboard, so
the only safe edit is read -> modify -> write the whole thing back.
"""
import asyncio
import json
import os
import sys

import websockets

URL = os.environ["HA_URL"].rstrip("/").replace("https://", "wss://").replace(
    "http://", "ws://") + "/api/websocket"
TOK = os.environ["HA_TOKEN"]


async def run(cmd, url_path, path=None):
    async with websockets.connect(URL, max_size=32 * 1024 * 1024) as ws:
        hello = json.loads(await ws.recv())
        assert hello["type"] == "auth_required", hello
        await ws.send(json.dumps({"type": "auth", "access_token": TOK}))
        ok = json.loads(await ws.recv())
        if ok.get("type") != "auth_ok":
            print("AUTH FAILED:", ok, file=sys.stderr)
            sys.exit(1)

        if cmd == "get":
            msg = {"id": 1, "type": "lovelace/config", "url_path": url_path}
        elif cmd == "list":
            msg = {"id": 1, "type": "lovelace/dashboards/list"}
        else:
            cfg = json.load(open(path))
            msg = {"id": 1, "type": "lovelace/config/save",
                   "url_path": url_path, "config": cfg}
        await ws.send(json.dumps(msg))
        while True:
            r = json.loads(await ws.recv())
            if r.get("id") == 1:
                break
        if not r.get("success", False):
            print("FAILED:", json.dumps(r)[:800], file=sys.stderr)
            sys.exit(1)
        print(json.dumps(r.get("result"), indent=2))


if __name__ == "__main__":
    c = sys.argv[1]
    up = sys.argv[2] if len(sys.argv) > 2 else None
    fp = sys.argv[3] if len(sys.argv) > 3 else None
    asyncio.run(run(c, up, fp))
