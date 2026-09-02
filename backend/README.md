# ecu-telemetry backend

FastAPI ingestion service + control plane + dashboard for the simulated ECU fleet.
Wire format and endpoints are the binding contracts in `../docs/protocol.md` and `../docs/interfaces.md`.

```
../run.sh                      # venv in backend/.venv, http://localhost:8780 (UDP :8781, TCP :8782)
cd backend && .venv/bin/python -m pytest -q
docker build -t ecu-telemetry backend && docker run -p 8780:8780 -p 8781:8781/udp -p 8782:8782 ecu-telemetry
```

## Architecture

```
ecu_agent ──UDP TELEMETRY──▶ ingest.py ─┬─▶ db.py (SQLite WAL, batched 0.5 s)  ──▶ telemetry / rollup_1m / logs / ...
          ◀─TCP control────▶            ├─▶ anomaly.py (rules + EWMA)          ──▶ alerts
                                        └─▶ bus.py (broadcast)                 ──▶ /api/events/stream (SSE) ──▶ static/ UI
                                 api.py  ◀── HTTP (config / fw / fault / cmd) ──▶ ingest.send() pushes frames to device
                          diagnostics.py ◀── POST /diagnose  (Claude via Anthropic SDK, heuristic fallback)
```

| module | role |
|---|---|
| `app/protocol.py` | stdlib-only frame codec (`struct` + `zlib.crc32`), dataclass per message, fault-flag names ⇄ bits, fw version u32 ⇄ "M.m.p", `StreamDecoder` for the TCP byte stream. Importable standalone by `sim/` and tests. |
| `app/db.py` | schema + queries. Telemetry inserts are buffered and flushed every 0.5 s in one transaction (20 devices × 10 Hz is trivial). `rollup()` folds complete minutes into `telemetry_rollup_1m` (avg/min/max per metric); `prune()` drops raw rows older than `ECU_RAW_RETENTION_H` (24 h). Rollups are kept forever. |
| `app/ingest.py` | asyncio UDP `DatagramProtocol` + TCP server. Per-device stats (frames, bad CRC, seq gaps → rolling loss % over the last 200 frames, reconnects, last seen). Writer registry lets the API push `CONFIG_SET` / `FW_UPDATE` / `FAULT_INJECT` / `CMD`. On `HELLO`: if the stored config version is newer than the device's, `CONFIG_SET` is pushed automatically; an unfinished OTA job is resumed (re-pushed, or marked `complete` if the device came back on the target version). Offline after 15 s without HEARTBEAT/TELEMETRY. |
| `app/anomaly.py` | rule engine (see below). |
| `app/diagnostics.py` | AI-assisted diagnosis with deterministic fallback (see below). |
| `app/api.py` / `app/main.py` | all endpoints from `interfaces.md`, SSE, CORS open, lifespan wiring, static dashboard. `create_app(db_path, udp_port, tcp_port)` exists for tests (port 0 = ephemeral). |
| `static/` | dashboard: hand-rolled canvas charts, no build step, no external deps, works offline. |

## Environment

| var | default | meaning |
|---|---|---|
| `ECU_HTTP_PORT` / `PORT` | 8780 | HTTP (used by `run.sh`) |
| `ECU_UDP_PORT` | 8781 | telemetry ingest |
| `ECU_TCP_PORT` | 8782 | control channel |
| `ECU_BIND` | 0.0.0.0 | bind address for UDP/TCP |
| `ECU_DB_PATH` | `backend/data/telemetry.db` | SQLite file (WAL) |
| `ECU_RAW_RETENTION_H` | 24 | raw telemetry retention |
| `ANTHROPIC_API_KEY` | unset | enables Claude diagnostics; unset → heuristic |
| `ECU_DIAG_MODEL` | `claude-opus-5` | model id for diagnostics |

## API summary

```
GET  /health                                {"status","devices_online","uptime_s"}
GET  /api/stats                             fleet counters
GET  /api/devices                           fleet summary (state, online, fw, loss_pct, latest{...}, open_alerts)
GET  /api/devices/{id}                      detail: stats, config (+history), fw_job, fault_injections, alerts, diagnoses
GET  /api/devices/{id}/telemetry?since&until&limit&step=raw|10s|1m   (1m uses the rollup table + live tail)
GET  /api/devices/{id}/logs?limit=
GET  /api/devices/{id}/config  PUT  → new version, CONFIG_SET pushed (ack_status: queued|sent|applied|rejected)
GET  /api/firmware  POST {version,notes,size_bytes}
POST /api/devices/{id}/firmware {version} → 202 job ; GET /api/devices/{id}/firmware/status
POST /api/devices/{id}/fault {set:[names],clear:[names],duration_ms}   (409 if device TCP not connected)
POST /api/devices/{id}/cmd {cmd:reboot|clear_faults|request_hello}
GET  /api/alerts?device=&status=open|acked|resolved|all&limit=   POST /api/alerts/{id}/ack
GET  /api/rules   PUT /api/rules (JSON, merged over defaults, persisted)
POST /api/devices/{id}/diagnose   GET /api/devices/{id}/diagnoses
GET  /api/events/stream           SSE: hello | telemetry (≤2/s per device) | alert | device | log
GET  /                            dashboard
```

## Alerts & anomaly detection (`anomaly.py`)

Evaluated on every telemetry sample, with the device's current config providing the limits:

* **Thresholds** – `temp_c > temp_limit_c` (warn), `> temp_limit_c+15` (critical), `current_a > current_limit_a`, `rpm > rpm_limit`, `voltage_v < 80` (96 V pack, 88–100 V open-circuit), `vibration_g > 2.5`. Values may be numbers or `config_key[+/-offset]`.
* **Fault flags** – any set bit opens an alert named `flag:<NAME>` with a per-flag severity.
* **Comm** – rolling `loss_pct > 10` → `packet_loss` (warn). **Offline** → `offline` (critical), resolved immediately when the device is seen again.
* **Rate of change** – `temp_c` rising faster than 2 °C/s → `rate:temp_c`.
* **Statistical** – per device × metric EWMA mean/variance (α=0.05, 20-sample warm-up, variance floor). |z| > 4 for 3 consecutive samples → `anomaly:<metric>`. Anomalous samples update the baseline at α/10 so an outlier can't hide itself, while a persistent level shift is eventually re-learned.

Dedupe: exactly one open (or acked) alert per (device, rule). Auto-resolve when the condition has been clear for `resolve_after_s` (10 s; checked every 5 s). All of this lives in one JSON document editable via `PUT /api/rules` (partial documents are merged over the defaults).

## Diagnostics (`diagnostics.py`)

`POST /api/devices/{id}/diagnose` builds a compact JSON context (device meta, config, fw job, latest sample, active faults, 5-min per-metric avg/min/max, comm stats, open + recently resolved alerts, last 30 logs, recent fault injections) and asks Claude (`claude-opus-5`, adaptive thinking, JSON-schema structured output, server-side refusal fallback) for:

```json
{"summary": "...", "probable_causes": [{"cause","evidence","likelihood"}], "recommended_actions": ["..."], "confidence": 0.0-1.0}
```

If `ANTHROPIC_API_KEY` is unset, or the call fails for any reason, the **heuristic diagnostician** runs instead: pattern rules map alert/flag combinations (e.g. overtemp + overcurrent → thermal runaway; temp rate → cooling loss; recent `OVERTEMP` injection → "SIL-injected test fault") to causes with normalised likelihoods and de-duplicated actions. Results carry `model` (`claude-opus-5` or `heuristic`) and are stored in the `diagnoses` table.

## Tests

`tests/test_protocol.py` hand-builds frames byte-by-byte from `protocol.md` (header offsets, CRC, payload sizes, resync). `tests/test_api.py` boots the real app with UDP/TCP on ephemeral ports, fires synthetic UDP frames (with seq gaps and a corrupted CRC), and runs a fake TCP device through HELLO → config push/ack → fault inject/ack → CMD → OTA → reboot/reconnect → auto config re-push, plus SSE and offline detection.
