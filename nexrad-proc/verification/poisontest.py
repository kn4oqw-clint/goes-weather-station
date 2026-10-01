#!/usr/bin/env python3
"""Prove the poison-message path: inject a volume that can never succeed.

The unit points at a MinIO key that does not exist, so `process()` fails at the
fetch stage every time -- the same shape of permanent failure a truncated Zarr
produces. Before this fix the entry would sit pending and be re-delivered by
xautoclaim forever, holding a consumer slot.

Expected: MAX_DELIVERIES attempts, then one dead-letter entry, an ACK, and
scit_poison_total incremented. The stream must end with 0 pending.
"""
import json
import os
import sys
import time

import redis

IN_STREAM = "nexrad:zarr"
GROUP = "scit"
DLQ = "nexrad:cells:dead"
r = redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"),
                   decode_responses=True)

unit = {"site": "KPOISON", "volume_utc": "2026-07-27T00:00:00Z",
        "volume_epoch": 1785110400, "vcp": 212,
        "bucket": "nexrad-zarr", "key": "does/not/exist/poison.zip"}

before_dlq = r.xlen(DLQ) if r.exists(DLQ) else 0
msg_id = r.xadd(IN_STREAM, {"unit": json.dumps(unit)})
print(f"injected {msg_id}; dlq before = {before_dlq}")

deadline = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 240)
while time.time() < deadline:
    time.sleep(5)
    pend = r.xpending(IN_STREAM, GROUP)
    n = pend.get("pending", 0) if isinstance(pend, dict) else 0
    dlq = r.xlen(DLQ) if r.exists(DLQ) else 0
    if dlq > before_dlq:
        print(f"DEAD-LETTERED after ~{int(time.time()-(deadline-240))}s; "
              f"pending now {n}, dlq {dlq}")
        for eid, f in r.xrange(DLQ, count=5)[-1:]:
            print("  dlq entry:", eid, f.get("deliveries"), f.get("dropped_at"))
        sys.exit(0)
    print(f"  pending={n} dlq={dlq}")
print("TIMEOUT: not dead-lettered yet (retries are paced by CLAIM_MIN_IDLE_MS)")
sys.exit(1)
