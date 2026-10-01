#!/usr/bin/env python3
"""Prove the GLM proximity query still works, by finding real lightning.

A proximity sensor that always returns zero is indistinguishable from a quiet
sky. The only way to tell them apart is to point the SAME query at somewhere
lightning is definitely happening right now and confirm it lights up.

Finds the densest recent flash cluster on the globe, runs the production
proximity query there, then runs it at home for comparison.
"""
import os
import sys

import psycopg2


def load_env(path=os.path.expanduser("~/.glm_lightning.env")):
    """Parse the env file the way systemd's EnvironmentFile does.

    Do NOT `set -a; . file` this: GLM_PG_DSN is an unquoted libpq DSN with
    spaces in it, so the shell assigns only `host=...` and drops the password,
    which fails with a confusing `fe_sendauth: no password supplied`. systemd
    takes the whole line after the first `=` as the value; so does this.
    """
    env = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


E = load_env()
DSN = E.get("GLM_PG_DSN") or os.environ["GLM_PG_DSN"]
LAT = float(E.get("STATION_LAT", os.environ.get("STATION_LAT", "30.643444")))
LON = float(E.get("STATION_LON", os.environ.get("STATION_LON", "-87.054500")))
WIN = int(E.get("GLM_WINDOW_MIN", os.environ.get("GLM_WINDOW_MIN", "30")))

PROX = """
SELECT
  MIN(ST_Distance(geom, ref)) / 1609.344                       AS nearest_mi,
  COUNT(*) FILTER (WHERE ST_DWithin(geom, ref, 10 * 1609.344)) AS n10,
  COUNT(*) FILTER (WHERE ST_DWithin(geom, ref, 30 * 1609.344)) AS n30,
  MAX(flash_time)                                              AS last_flash
FROM glm_flashes,
     LATERAL (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography AS ref) r
WHERE flash_time > now() - interval '%s minutes'
  AND ST_DWithin(geom, ref, 30 * 1609.344);
"""

with psycopg2.connect(DSN) as c, c.cursor() as cur:
    cur.execute("SELECT count(*), min(flash_time), max(flash_time), now() "
                "FROM glm_flashes WHERE flash_time > now() - interval '%s minutes'",
                (WIN,))
    n, lo, hi, now = cur.fetchone()
    print(f"glm_flashes in last {WIN} min : {n}")
    print(f"  window   : {lo} .. {hi}")
    age = (now - hi).total_seconds() if hi else None
    print(f"  ingest age: {age:.0f}s" if age is not None else "  ingest age: NO DATA")
    if not n:
        print("\nNo flashes anywhere in the window — cannot validate. "
              "Either the ingestor is down or the globe is genuinely quiet.")
        sys.exit(1)

    # Densest 1-degree bin in the window == somewhere it is certainly storming.
    cur.execute("""
        SELECT round(ST_Y(geom::geometry)::numeric, 0) AS la,
               round(ST_X(geom::geometry)::numeric, 0) AS lo,
               count(*) AS n
        FROM glm_flashes
        WHERE flash_time > now() - interval '%s minutes'
        GROUP BY 1, 2 ORDER BY n DESC LIMIT 3;
    """, (WIN,))
    hot = cur.fetchall()
    print("\nbusiest 1-degree bins (lat, lon, flashes):")
    for la, lo_, cnt in hot:
        print(f"  {la:>6}, {lo_:>7}   {cnt}")

    print(f"\n{'location':<26}{'nearest_mi':>12}{'10mi':>8}{'30mi':>8}  last_flash")
    print("-" * 74)
    checks = [("HOME (station)", LAT, LON)]
    if hot:
        checks.append((f"ACTIVE STORM {hot[0][0]},{hot[0][1]}",
                       float(hot[0][0]), float(hot[0][1])))
    for name, la, lo_ in checks:
        cur.execute(PROX, (lo_, la, WIN))
        near, n10, n30, last = cur.fetchone()
        nn = f"{near:.1f}" if near is not None else "None"
        print(f"{name:<26}{nn:>12}{n10:>8}{n30:>8}  {last}")

print("\nIf ACTIVE STORM shows strikes and HOME shows none, the query is sound "
      "and the zeros at home are real.")
