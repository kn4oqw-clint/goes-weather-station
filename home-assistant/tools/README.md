# Home Assistant tooling

Scripts that push config into the live HA at `10.10.0.5:8123` and pull it back
out. All of them need `HA_URL` and `HA_TOKEN`; the token lives in
`~/.glm_lightning.env` on the wxstation Pi (`10.10.0.171`, mode `0600`).

```bash
export HA_URL=http://10.10.0.5:8123
export HA_TOKEN=$(ssh cchance@10.10.0.171 'grep "^HA_TOKEN=" ~/.glm_lightning.env | cut -d= -f2-')
```

⚠️ **Do not `set -a; . ~/.glm_lightning.env`.** `GLM_PG_DSN` is an unquoted
libpq DSN containing spaces, so the shell assigns only `host=...` and silently
drops the password — you get `fe_sendauth: no password supplied`, which looks
like a credentials problem and is not. systemd's `EnvironmentFile` takes the
whole line after the first `=`; `glm_validate.py` parses it the same way.

| script | direction | what it does |
|---|---|---|
| `install_lightning.py` | repo → HA | Writes the 5 lightning automations via `/api/config/automation/config/<id>` and reloads. Idempotent — re-running overwrites by id. `--dry-run` to preview. |
| `export_automations.py` | HA → repo | Dumps the live automations back to `../lightning-automations.yaml`, so the repo copy cannot drift from what is running. |
| `lovelace.py` | both | `get`/`save`/`list` a storage-mode dashboard over the WebSocket API. |
| `add_lightning_section.py` | transform | Inserts the Lightning section into a dashboard JSON dump. Idempotent — replaces an existing Lightning section rather than duplicating it. |
| `glm_validate.py` | read-only | Proves the proximity query works by finding real lightning. |
| `test_push.py` | → phones | Sends a labelled test notification to both handsets. **Reuses the real alert `data` payload** (Android `channel: lightning` + `importance: high`, iOS `time-sensitive`) — a generic test push would only prove the phone is online, not that a 2 a.m. alert clears Do Not Disturb. |

## Dashboards

Storage-mode dashboards are **not** reachable through the REST API, and editing
`.storage` by hand does not work — HA holds the config in memory and will
overwrite the file. The WebSocket API is the supported route:

```bash
python lovelace.py get weather-station > dash.json     # ALWAYS back up first
python add_lightning_section.py dash.json dash.new.json
python lovelace.py save weather-station dash.new.json
```

`lovelace/config/save` **replaces the entire dashboard**. Read → modify → write
the whole thing back; `weather-station-dashboard.backup.json` here is the
pre-change snapshot from 2026-07-27 and is the undo.

## Validating lightning

```bash
ssh cchance@10.10.0.171 'python3 /tmp/glm_validate.py'
```

Run this after any change to the proximity query. **A sensor that always returns
zero is indistinguishable from a quiet sky**, so the check finds the densest
recent flash cluster on the globe and runs the production query there as well as
at home. Result from 2026-07-27:

```
glm_flashes last 30 min : 46,814     ingest age: 33s
ACTIVE STORM 40,-86   nearest 7.4 mi   6 within 10mi   1906 within 30mi
HOME (station)        none             0               0
```

Lighting up over Indiana while staying dark at home is what proves the home
zeros are real.
