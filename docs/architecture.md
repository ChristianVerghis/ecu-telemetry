# Architecture

```
 ┌──────────────────────────┐   UDP :8781  (TELEMETRY, 10 Hz, seq+CRC32)
 │ ecu_agent  (C++17)       │ ───────────────────────────────────────►  ┌─────────────────────────────┐
 │  physics model           │                                           │ backend (FastAPI + asyncio)  │
 │  fault injection         │   TCP :8782  (HELLO/HEARTBEAT/LOG ►)      │  ingest.py  UDP+TCP servers  │
 │  OTA / config / logging  │ ◄───────────────────────────────────────► │  db.py      SQLite WAL       │
 │  reconnect w/ backoff    │   (◄ CONFIG_SET/FW_UPDATE/FAULT/CMD)      │  anomaly.py rules + EWMA     │
 └──────────────────────────┘                                           │  diagnostics.py Claude/heur. │
        ▲   ▲   ▲                                                       │  api.py     REST + SSE       │
        │   │   │  N processes (docker compose / sim/fleet.py)          └──────────────┬──────────────┘
 ┌──────┴───┴───┴───────────┐                                                          │ HTTP :8780
 │ sim/run_sil.py           │  drives timeline via REST, asserts expectations          ▼
 │ sim/lossy_proxy.py       │  UDP chaos (drop/delay/reorder/dup)             ┌─────────────────┐
 │ sim/scenarios/*.yml      │                                                 │ static/ dashboard│
 └──────────────────────────┘                                                 └─────────────────┘
```

## Layers
| Layer | Where | Notes |
|---|---|---|
| Device / ECU | `firmware/src/physics.cpp` | Deterministic motor + battery + thermal + vibration + GPS model, drive-cycle profiles, fault flags mutate the model. |
| Telemetry agent | `firmware/src/agent.cpp` | Fixed-rate loop, UDP sender, select()-based TCP control client, backoff reconnect, simulated OTA. |
| Protocol | `docs/protocol.md`, `firmware/src/protocol.cpp`, `backend/app/protocol.py` | 36-byte header, LE, CRC-32 trailer. Same codec in both languages, cross-tested with byte vectors. |
| Ingestion | `backend/app/ingest.py` | asyncio DatagramProtocol + StreamReader control server; seq-gap packet-loss estimator; offline detector. |
| Storage | `backend/app/db.py` | SQLite WAL, batched inserts, raw 24 h retention, 1-minute rollups. |
| Alerts | `backend/app/anomaly.py` | Threshold rules (editable), EWMA z-score anomaly, rate-of-change, dedupe + auto-resolve. |
| Diagnostics | `backend/app/diagnostics.py` | Context pack → Claude → strict JSON; heuristic fallback when no API key. |
| Dashboard | `backend/static/` | Vanilla JS, SSE live updates, canvas charts, config/OTA/fault-injection/diagnose panels. |
| SIL | `sim/` | Scenario-driven fleets, fault timelines, expectations, chaos proxy. |
| Delivery | `docker-compose.yml`, `.github/workflows/` | Containerized backend + fleet, CI matrix, tagged releases. |

## Data flow for one alert
1. Agent injects OVERTEMP (CLI schedule or FAULT_INJECT from API) → thermal model heats → `temp_c` climbs.
2. TELEMETRY frame → UDP → decode/CRC → buffered insert → `anomaly.evaluate(device, sample)`.
3. Threshold rule `temp_c > temp_limit_c` opens an alert (deduped per device+rule) → SSE `alert` event → dashboard drawer.
4. Condition clears for 10 s → alert auto-resolved.
5. Operator hits **Diagnose** → context pack (stats, alerts, logs, config, fw, faults) → Claude → structured causes/actions.
