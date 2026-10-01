# ecu-telemetry build log

**Update 2026-09-01**: scaffolded the repo layout.

**Update 2026-09-01**: built the whole chain in one session.
- `docs/protocol.md` + `docs/interfaces.md` written first as the contract; firmware, backend and sim were then built in parallel against it.
- `firmware/`: dependency-free C++17 agent (CMake, 15 CTest tests). Deterministic plant model (motor/battery/thermal/vibration/GPS, 4 drive profiles, 8 fault flags), UDP telemetry with seq + CRC-32, select()-based TCP control client, backoff reconnect, simulated OTA with reboot, versioned config persisted to `<cfg>.applied`, LOG frame shipping.
- `backend/`: FastAPI + asyncio UDP/TCP servers, SQLite WAL with batched inserts + 1-min rollups + retention, threshold/EWMA/rate-of-change alert engine with dedupe + auto-resolve, Claude-backed diagnostics with heuristic fallback, SSE, vanilla-JS control-room dashboard. 24 pytest tests.
- `sim/`: SIL runner with 6 YAML scenarios + expectations, lossy UDP proxy with live control endpoint, fleet launcher. `tests/integration`: real agent ↔ real backend.
- `docker-compose.yml` (backend + 4 ECUs, `chaos` profile), GitHub Actions CI (firmware/backend/integration/SIL/docker) + tagged release workflow, Makefile.
- Integration fixes: aligned undervoltage rule to the firmware's 96 V pack (was 48 V), `pythonpath` in pytest.ini so tests run from repo root, thermal_runaway scenario timing relaxed (60 s thermal time constant means recovery takes ~40 s).
- Not verified locally: Docker builds (daemon not running). GitHub remote not yet created.

**Update 2026-10-01** (nightly): MQTT transport option alongside raw UDP.
- `backend/app/mqtt_bridge.py`: opt-in `MqttBridge` (paho-mqtt) that republishes each downsampled telemetry frame to `<prefix>/<device_id>/telemetry` as JSON. Disabled unless `ECU_MQTT_ENABLED=1`; `from_env()` reads `ECU_MQTT_HOST`/`ECU_MQTT_PORT`/`ECU_MQTT_TOPIC_PREFIX` and swallows connect failures (logs + returns `None`) so a missing/unreachable broker never takes the ingestion service down.
- `EventBus` takes an optional `mqtt_bridge` and forwards the same already-downsampled (≤2 Hz/device) telemetry it sends over SSE — one source of truth for what counts as a "frame" for the UI and for MQTT.
- `main.py` wires `mqtt_bridge.from_env()` into the `EventBus` at startup and closes it on shutdown. No behavior change with the env var unset (the default).
- Added `paho-mqtt>=2.1` to `backend/requirements.txt`, installed in `.venv`.
- Tests: `backend/tests/test_mqtt_bridge.py`, 11 new cases — topic formatting, JSON payload, client-error swallowing, `from_env` disabled/enabled/unreachable-broker paths (all via an injected fake client, no real broker needed), and `EventBus` forwarding only on the downsampled publish. 34/34 backend tests pass (`test-backend` action). `python -c "from app.main import create_app; create_app()"` clean.
- Not tested against a real MQTT broker (none running on this Mac) — the fake-client tests cover the bridge's own logic; an end-to-end check against mosquitto is left for the owner if/when it matters.
