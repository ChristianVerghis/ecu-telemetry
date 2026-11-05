# ecu-telemetry

**A self-contained connected-vehicle telemetry stack: C++ ECU agents (a simulated fleet plus one real Jetson board) stream a binary protocol into a FastAPI backend with time-series storage, live dashboard, alerting, OTA, and a software-in-the-loop test harness.**

## What it is

The pipeline, end to end:

- **Device / ECU** (`firmware/`): a dependency-free C++17 agent that behaves like a motor-controller ECU. It runs a deterministic plant model (motor, battery, thermal, vibration, GPS) with four drive-cycle profiles and eight injectable fault flags, streams `TELEMETRY` frames over UDP at a fixed rate, and holds a TCP control connection with exponential-backoff reconnect. With `--source jetson` the same binary runs on a Jetson Orin Nano and reports the board's measured temperature, input voltage and current, and fan speed instead of the plant model's.
- **Wire protocol** (`docs/protocol.md`): one frame format for both transports. 36-byte little-endian header (magic, version, message type, device ID, sequence number, timestamp, firmware version, payload length) plus a CRC-32 trailer. Implemented once in C++ and once in Python, cross-tested with shared byte vectors.
- **Ingestion and storage** (`backend/app/ingest.py`, `db.py`): asyncio UDP datagram server and TCP stream server. Packet loss is estimated from sequence gaps; devices go offline after 15 s of silence. Samples land in SQLite (WAL mode) via batched inserts, with 1-minute rollups and 24 h raw retention.
- **Alerts** (`backend/app/anomaly.py`): editable threshold rules, EWMA z-score anomaly detection, and rate-of-change rules, with per-device deduplication, auto-resolve after the condition clears, and per-device threshold overrides (a 5 V board and a 96 V pack do not share an undervoltage limit).
- **Control plane**: versioned device configuration pushed over the TCP channel (`CONFIG_SET`), simulated firmware OTA (`FW_UPDATE` / `FW_ACK` / reboot / `HELLO`), remote fault injection, and `LOG` frames shipped to a per-device log store.
- **Dashboard** (`backend/static/`): vanilla JS control-room UI over the REST API with server-sent events for live updates, canvas charts, and panels for config, OTA, fault injection, and diagnostics.
- **Diagnostics** (`backend/app/diagnostics.py`): builds a context pack (recent stats, alerts, logs, config, firmware, faults) and asks Claude for structured causes and actions when `ANTHROPIC_API_KEY` is set; otherwise a rule-based heuristic diagnostician answers.
- **SIL harness** (`sim/`): `run_sil.py` spawns the real agent binary per scenario device, points it at the real backend, drives a timeline through the HTTP API (faults, config pushes, OTA, reboots), and asserts expectations. `lossy_proxy.py` adds drop, delay, jitter, reorder, and duplication on the UDP path, adjustable live. Six YAML scenarios ship in `sim/scenarios/`.

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

## Why I built it

Working on EV and automotive systems means living at the seam between firmware on a controller and the services that collect, store, and act on what it reports. Most of that stack is normally spread across teams and vendors, so I wanted one small repo that exercises the whole chain honestly: a device with real physics and real failure modes, a binary protocol with sequence numbers and checksums, a backend that has to cope with loss and reconnects, and a test harness that runs the actual binaries rather than mocks. Most of the fleet is simulated and one device is real: a Jetson Orin Nano that reports its own junction temperature and power rail. The interfaces and failure handling are the same ones a real fleet needs.

## Status

As of 2026-11-05:

**Works**
- Firmware agent builds with CMake and passes its 18 CTest tests (CRC, header layout, codec round-trips, fault parsing, config parsing, physics determinism and fault behaviour, the sysfs sensor reader against a fixture tree, plus CLI and offline-run smoke tests) on macOS and in CI on Linux; it also builds natively on the Jetson (arm64).
- Backend passes 25 pytest tests (protocol, db, anomaly, API); integration tests run a real agent against a real backend.
- All six SIL scenarios validate; `baseline_fleet` and `thermal_runaway` run in CI on every push.
- Fleet launcher, lossy proxy, dashboard, alerts, config push, simulated OTA, and log shipping all exercised end to end locally.
- Dashboard charts cover 5 m (raw), 1 h (10 s buckets), 24 h and 7 d (1-minute rollups, which are kept after raw samples are pruned at 24 h).
- A real device: the agent runs on a Jetson Orin Nano Super with `--source jetson`, and a real thermal event has gone through the whole chain (see below).
- GitHub Actions CI (firmware, backend, integration, SIL matrix, compose build) and a tagged release workflow (Linux amd64/arm64 binaries, GHCR images).

**Rough edges**
- Docker images build in CI (`docker compose build`) but have not been run locally or on the Jetson.
- On the Jetson, vibration, speed, and position have no sensor behind them and report 0; fan rpm is estimated from PWM duty because the fan has no tachometer.
- No MQTT transport, no alert notifications (webhook/email), and no microcontroller port. The agent is written to port to an ESP32/STM32 with lwIP sockets, but that has not been done.

## Real hardware target

The first real device is a Jetson Orin Nano Super. The backend stays on a laptop; the board builds the agent natively (arm64, about 5 s, no code changes) and streams over Tailscale at 10 Hz. `make jetson-deploy` syncs the source, builds, runs CTest on the board, and restarts the agent.

With `--source jetson` the agent reads the board instead of the plant model: junction temperature from `/sys/class/thermal`, input voltage and current from the INA3221 power monitor, and fan duty as rpm. Device-side limits, fault flags, and state then run on measured values.

One real event, start to finish: a config push lowered `temp_limit_c` to 52 °C over the control channel, then all six cores and the GPU were loaded for 75 s. Junction temperature went from 48.5 to 54.8 °C, the device raised `OVERTEMP` and entered `FAULT`, the backend opened `temp_high` and `flag:OVERTEMP`, and both alerts resolved on their own about three minutes later once the board had cooled.

Two changes came out of running on hardware. Device-side thresholds gained hysteresis, because a board hovering at exactly its limit produced 13 set/clear pairs in a minute. Backend threshold rules gained per-device overrides, because the fixed 80 V undervoltage rule was permanently true for a 5 V board. Details in [docs/hardware.md](docs/hardware.md).

## Stack

- **Firmware**: C++17, CMake 3.16+, CTest, POSIX sockets, no third-party dependencies.
- **Backend**: Python 3.13, FastAPI, uvicorn, asyncio, Pydantic v2, SQLite (WAL), `anthropic` SDK (optional at runtime).
- **Dashboard**: vanilla JavaScript, server-sent events, canvas.
- **Simulation**: Python stdlib + PyYAML + httpx.
- **Tests**: CTest, pytest, pytest-asyncio.
- **Delivery**: Docker Compose (backend + 4 ECUs, `chaos` profile adds the lossy proxy), GitHub Actions.

## Run it

The whole thing in one command: a fresh database, the backend, and six simulated ECUs. It prints the dashboard URL, then walks a three-minute story and narrates it in the terminal: a thermal fault and its alerts, a diagnosis, a config push that trips a device's own protection, a staggered OTA rollout, and a link losing 30 % of its packets. Each step asserts what it claims (21 checks). The fleet then stays up until Ctrl-C.

```bash
make demo
```

It uses ports 8780-8782 when they are free and says so when it has to move to 8790-8792.

The pieces, one at a time. Backend (creates `backend/.venv` on first run, serves on http://localhost:8780):

```bash
./run.sh
```

Firmware agent:

```bash
cmake -S firmware -B firmware/build -DCMAKE_BUILD_TYPE=Release && cmake --build firmware/build -j
./firmware/build/ecu_agent --id ECU-0001 --profile city --fault OVERTEMP@30:40
```

A fleet of six agents against the running backend:

```bash
backend/.venv/bin/python sim/fleet.py --n 6
```

A SIL scenario with assertions (starts its own backend):

```bash
backend/.venv/bin/python sim/run_sil.py sim/scenarios/thermal_runaway.yml --start-backend
```

Everything in containers:

```bash
docker compose up --build                  # backend + ECU-0001..0004
docker compose --profile chaos up --build  # adds lossy proxy + ECU-0005 routed through it
```

Tests:

```bash
ctest --test-dir firmware/build --output-on-failure     # C++ unit tests
backend/.venv/bin/python -m pytest -q backend/tests     # protocol / db / anomaly / API
backend/.venv/bin/python -m pytest -q tests/integration # real agent <-> real backend
```

`make help` lists the equivalent Make targets (`build`, `test`, `demo`, `sil`, `sil-all`, `fleet`, `up`, `clean`, `jetson-deploy`, `jetson-logs`).

Set `ANTHROPIC_API_KEY` to enable Claude-backed diagnostics; without it the heuristic diagnostician answers and results are tagged `model: "heuristic"`.

Further docs: [docs/protocol.md](docs/protocol.md) (wire format), [docs/interfaces.md](docs/interfaces.md) (CLI / REST / SIL contracts), [docs/architecture.md](docs/architecture.md) (layers and data flow), [docs/hardware.md](docs/hardware.md) (the Jetson target), plus `firmware/README.md`, `backend/README.md`, `sim/README.md`.

## Layout

- `firmware/` — C++17 ECU agent: plant model, protocol codec, UDP/TCP client, OTA/config, CTest suite, Dockerfile.
- `backend/` — FastAPI service: ingestion servers, SQLite storage, alert engine, diagnostics, REST + SSE API, static dashboard, pytest suite, Dockerfile.
- `sim/` — SIL runner, fleet launcher, lossy UDP proxy, YAML scenarios.
- `tests/` — integration tests that run the real agent against the real backend.
- `docs/` — protocol spec, interface contracts, architecture notes, hardware notes.
- `deploy/jetson/` — user systemd service, environment file, and installer for the board.
- `.github/workflows/` — CI and tagged-release pipelines.
- `docker-compose.yml`, `Makefile`, `run.sh` — local entry points.

## License

MIT. See [LICENSE](LICENSE).
