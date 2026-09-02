# ecu-telemetry

A miniature connected-vehicle / embedded telemetry platform, end to end:

**simulated ECU (C++) → binary UDP/TCP protocol → FastAPI ingestion → SQLite time-series → live dashboard → alerts & anomaly detection**, plus firmware versioning/OTA, versioned device config, remote logging, packet-loss handling, containerized backend, CI/CD, a scenario-driven SIL environment with fault injection, and AI-assisted diagnostics.

## Quick start
```bash
# 1. backend (creates backend/.venv on first run) → http://localhost:8780
./run.sh

# 2. firmware agent
cmake -S firmware -B firmware/build -DCMAKE_BUILD_TYPE=Release && cmake --build firmware/build -j
./firmware/build/ecu_agent --id ECU-0001 --profile city --fault OVERTEMP@30:40

# 3. or a whole fleet
backend/.venv/bin/python sim/fleet.py --n 6

# 4. or a SIL scenario with assertions
backend/.venv/bin/python sim/run_sil.py sim/scenarios/thermal_runaway.yml

# 5. or everything in containers
docker compose up --build            # add --profile chaos for the lossy proxy
```

Set `ANTHROPIC_API_KEY` to enable Claude-backed diagnostics; without it a heuristic diagnostician answers.

## Tests
```bash
ctest --test-dir firmware/build --output-on-failure    # C++ unit tests
backend/.venv/bin/python -m pytest -q backend/tests    # protocol / db / anomaly / API
backend/.venv/bin/python -m pytest -q tests/integration # real agent ↔ real backend
```

## Docs
- [docs/protocol.md](docs/protocol.md) — wire format
- [docs/interfaces.md](docs/interfaces.md) — CLI / REST / SIL contracts
- [docs/architecture.md](docs/architecture.md) — layers and data flow
- `firmware/README.md`, `backend/README.md`, `sim/README.md`
