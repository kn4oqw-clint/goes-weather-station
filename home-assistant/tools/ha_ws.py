#!/usr/bin/env python3
"""Minimal Home Assistant WebSocket client (no third-party deps).

Lovelace storage-mode dashboards can ONLY be written over the WS API
(lovelace/config/save) -- the REST API does not expose them, and hand-editing
/config/.storage/lovelace.* needs a core restart. Neither `websockets` nor
`websocket-client` is installed here, so this speaks just enough RFC6455.

Usage:
  ha_ws.py list                       # dashboards
  ha_ws.py dump [url_path]            # print view/card structure
  ha_ws.py save <url_path> <file.json>
Credentials come from /etc/goes/ha.env (HA_URL, HA_TOKEN).
"""
import base64, json, os, re, socket, struct, sys, urllib.parse


def load_env(path="/etc/goes/ha.env"):
    env = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


class WS:
    def __init__(self, url):
        u = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=25)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /api/websocket HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        if b"101" not in buf.split(b"\r\n")[0]:
            raise RuntimeError("upgrade failed: " + buf.split(b"\r\n")[0].decode())
        self.buf = buf.split(b"\r\n\r\n", 1)[1]

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        """Return one complete text message, reassembling continuation frames."""
        payload = b""
        while True:
            b0, b1 = self._read(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            data = self._read(ln)              # server->client is never masked
            if opcode == 0x8:
                raise RuntimeError("server closed")
            if opcode == 0x9:                  # ping -> pong
                self._send_frame(data, 0xA)
                continue
            if opcode == 0xA:
                continue
            payload += data
            if fin:
                return json.loads(payload.decode())

    def _send_frame(self, data, opcode=0x1):
        hdr = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        self.sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send(self, obj):
        self._send_frame(json.dumps(obj).encode())


class HA:
    def __init__(self, url, token):
        self.ws = WS(url)
        assert self.ws.recv()["type"] == "auth_required"
        self.ws.send({"type": "auth", "access_token": token})
        r = self.ws.recv()
        if r["type"] != "auth_ok":
            raise SystemExit("auth failed: %s" % r)
        self.id = 0

    def cmd(self, **kw):
        self.id += 1
        kw["id"] = self.id
        self.ws.send(kw)
        while True:
            m = self.ws.recv()
            if m.get("id") == self.id and m.get("type") == "result":
                if not m.get("success"):
                    raise SystemExit("command failed: %s" % m.get("error"))
                return m.get("result")


def describe(cfg):
    for vi, v in enumerate(cfg.get("views", [])):
        print("  view[%d] title=%r path=%r cards=%d sections=%d"
              % (vi, v.get("title"), v.get("path"), len(v.get("cards") or []),
                 len(v.get("sections") or [])))
        for ci, c in enumerate(v.get("cards") or []):
            ident = c.get("entity") or c.get("title") or c.get("url") or ""
            print("      card[%d] %s %s" % (ci, c.get("type"), ident))
        for si, s in enumerate(v.get("sections") or []):
            for ci, c in enumerate(s.get("cards") or []):
                ident = c.get("entity") or c.get("title") or c.get("url") or ""
                print("      section[%d].card[%d] %s %s" % (si, ci, c.get("type"), ident))


if __name__ == "__main__":
    env = load_env()
    ha = HA(env["HA_URL"], env["HA_TOKEN"])
    act = sys.argv[1] if len(sys.argv) > 1 else "list"

    if act == "list":
        for d in ha.cmd(type="lovelace/dashboards/list"):
            print("%-28s %-22s mode=%s" % (d.get("url_path"), d.get("title"), d.get("mode")))
        print("\n(the default dashboard has url_path None)")

    elif act == "dump":
        up = sys.argv[2] if len(sys.argv) > 2 else None
        kw = {"type": "lovelace/config"}
        if up and up != "-":
            kw["url_path"] = up
        cfg = ha.cmd(**kw)
        print("dashboard %r:" % up)
        describe(cfg)
        json.dump(cfg, open("/tmp/lovelace_dump.json", "w"), indent=1)
        print("full config -> /tmp/lovelace_dump.json")

    elif act == "save":
        up, path = sys.argv[2], sys.argv[3]
        cfg = json.load(open(path))
        kw = {"type": "lovelace/config/save", "config": cfg}
        if up and up != "-":
            kw["url_path"] = up
        ha.cmd(**kw)
        print("saved dashboard %r" % up)
