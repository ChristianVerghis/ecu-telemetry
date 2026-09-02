# ecu_agent — simulated ECU telemetry firmware

A dependency-free C++17 agent that behaves like a motor-controller ECU: it runs a
deterministic plant model (motor / battery / thermal / vibration / GPS), streams
`TELEMETRY` over UDP, and speaks the TCP control protocol (`HELLO`, `HEARTBEAT`,
`LOG`, `CONFIG_SET`, `FW_UPDATE`, `FAULT_INJECT`, `CMD`) exactly as specified in
[`docs/protocol.md`](../docs/protocol.md) and [`docs/interfaces.md`](../docs/interfaces.md).

POSIX sockets only; builds with Apple clang (macOS) and gcc (Debian).

## Build & test

```sh
cmake -S firmware -B firmware/build
cmake --build firmware/build
ctest --test-dir firmware/build --output-on-failure
```

Targets: `ecu_agent` (executable), `ecu_core` (static lib), `ecu_tests` (CTest runner).

Docker (multi-stage, produces `/ecu_agent`):

```sh
docker build -t ecu-agent firmware
docker run --rm --network host ecu-agent --id ECU-0001 --host 127.0.0.1
```

## CLI

```
ecu_agent --id ECU-0001 [--host 127.0.0.1] [--udp-port 8781] [--tcp-port 8782]
          [--hz 10] [--fw 1.0.0] [--hw SIM-MOTOR-A] [--profile city|highway|idle|stress]
          [--drop-rate 0.0] [--seed 42] [--duration 0] [--config path.cfg]
          [--fault FLAGNAME@START_S:DUR_S]...  [--log-level info] [--no-gps]
```

| flag | meaning |
|------|---------|
| `--id` | device id, 1–12 printable ASCII chars (required) |
| `--host`, `--udp-port`, `--tcp-port` | backend address (UDP telemetry, TCP control) |
| `--hz` | telemetry rate, 1..1000 |
| `--fw` | firmware version string `MAJOR.MINOR.PATCH` (encoded as `(maj<<16)\|(min<<8)\|patch`) |
| `--hw` | hardware model string (≤15 chars) |
| `--profile` | drive cycle: `city` (stop-and-go), `highway` (steady cruise), `idle` (motor off), `stress` (near-limit load with full-throttle bursts) |
| `--drop-rate` | agent-side simulated UDP loss, 0..1. Sequence numbers still advance so the backend sees gaps |
| `--seed` | PRNG seed; the same seed + profile gives a bit-identical telemetry stream |
| `--duration` | run N seconds then exit 0 (0 = forever) |
| `--config` | `key=value` file: `hz`, `rpm_limit`, `temp_limit_c`, `current_limit_a`, `log_level`. Values applied via `CONFIG_SET` are written to `<path>.applied` |
| `--fault` | `NAME@START_S:DUR_S` — inject fault `NAME` at t=START for DUR seconds (`:DUR` omitted or 0 = until cleared). Repeatable. Names: `OVERTEMP OVERCURRENT UNDERVOLTAGE VIBRATION_HIGH SENSOR_STUCK GPS_LOST COMM_DEGRADED ENCODER_FAULT` |
| `--log-level` | `debug\|info\|warn\|error` — stderr threshold and the threshold for shipping `LOG` frames |
| `--no-gps` | report lat/lon as 0 (no `GPS_LOST` flag) |

Precedence: built-in defaults < `--config` file < explicit CLI flags < runtime `CONFIG_SET`.

Exit codes: `0` ok (duration elapsed or SIGINT/SIGTERM), `2` bad arguments. The agent
never exits on network failure.

Logs go to stderr as `[ISO-ts] [level] msg`; every record at or above the log level
is also shipped as a `LOG` frame while the TCP channel is connected.

## Runtime behaviour

* **Scheduling** — one `select()` loop on a monotonic clock. Telemetry ticks at `--hz`;
  the TCP socket is non-blocking so the telemetry loop never stalls on the network.
  `ts_ms` in frame headers comes from the system clock.
* **UDP telemetry** — one 48-byte `TELEMETRY` payload per tick, `seq` per transport.
* **TCP control** — connects, sends `HELLO`, then `HEARTBEAT` every 5 s. On any error it
  reconnects with exponential backoff 1 s → 30 s (±25 % jitter). While disconnected the
  `COMM_DEGRADED` flag is set in telemetry.
* **CONFIG_SET** — validated (hz 1..1000, log_level 0..3, positive limits), applied
  live (rate, log level, alert limits), persisted to `<config>.applied` when `--config`
  was given, then `CONFIG_ACK(applied)`; otherwise `CONFIG_ACK(rejected)`.
* **FW_UPDATE** — `FW_ACK(accepted)` → `FW_ACK(downloading)` every second for
  `max(1, image_size_bytes / 500000)` s → `FW_ACK(applied)` → TCP closed, 1 s simulated
  reboot (uptime resets, seq continues, injected faults cleared) → reconnect and `HELLO`
  with the new version. A target lower than the running version or malformed →
  `FW_ACK(rejected)`.
* **FAULT_INJECT** — set/clear bits with optional expiry; answered with `FAULT_ACK`
  carrying the currently active flags (also sent when a timed fault expires).
* **CMD** — `reboot` (as above, same firmware), `clear_faults` (drops all injected
  faults, replies `FAULT_ACK`), `request_hello` (re-sends `HELLO`).
* **Local alerts** — the device checks its own readings against `rpm_limit`,
  `temp_limit_c`, `current_limit_a`, an undervoltage threshold (80 V) and a vibration
  threshold (2.0 g). Each rising edge logs one `WARN` (shipped as a `LOG` frame), each
  clearing edge one `INFO`.

## Plant model (`physics.cpp`)

Deterministic for a given seed (own xorshift64* PRNG + Box–Muller, so results do not
depend on the C++ standard library). Defaults model a ~100 V, 6000 rpm traction motor.

| subsystem | model |
|-----------|-------|
| throttle | drive-cycle profile, 0..1, with small noise |
| motor | first-order lag `rpm += (throttle·rpm_max − rpm)·dt/τ`, τ = 1.5 s |
| current | `I = I_idle + k·throttle + k_a·max(0, d rpm/dt)` (torque demand) |
| battery | `V = V0(SoC) − I·R`, V0 88→100 V over SoC, R = 30 mΩ, SoC drains at 100 Ah |
| thermal | `C·dT/dt = I²R_w − h·(T − T_amb)`, R_w = 50 mΩ, C = 600 J/K, h = 10 W/K, T_amb = 25 °C |
| vibration | baseline 0.15 g + 0.4 g·(rpm/rpm_max)² + random decaying spikes + noise |
| speed | `rpm / gear_ratio(6) · wheel_circumference(2 m)` → kph |
| GPS | integrates heading and speed around a 500 m-radius loop from (37.7749, −122.4194), ±1.5 m noise |

Fault injection changes the plant, not just the flag:

| flag | effect |
|------|--------|
| `OVERTEMP` | +2 kW heater in the winding → temperature crosses the limit within ~20 s from cold |
| `OVERCURRENT` | +60 A on the phase current |
| `UNDERVOLTAGE` | V0 drops 20 V → bus falls under the 80 V threshold |
| `VIBRATION_HIGH` | +2.5 g RMS |
| `SENSOR_STUCK` | temperature *reading* frozen at the value when injected (plant keeps evolving) |
| `GPS_LOST` | lat/lon reported as 0 |
| `ENCODER_FAULT` | rpm reading gets ±15 % noise and 10 % dropouts to 0 |
| `COMM_DEGRADED` | flag only (normally owned by the agent's TCP state) |

Threshold detection runs on the *readings*, so a stuck sensor hides a real overtemp
just as it would on hardware. `fault_flags = injected | detected | comm`.

State machine: any of `OVERTEMP | OVERCURRENT | UNDERVOLTAGE | ENCODER_FAULT` → `FAULT`;
any other flag → `DEGRADED`; rpm < 50 and no throttle → `IDLE`; otherwise `RUNNING`.

## Layout

```
firmware/
  CMakeLists.txt
  Dockerfile
  include/ecu/
    protocol.hpp   frame codec, CRC-32, message structs, fault/state names
    physics.hpp    plant model + PRNG
    agent.hpp      main loop, UDP sender, TCP control client
    config.hpp     key=value config file
    log.hpp        stderr logger with LOG-frame sink
  src/             implementations + main.cpp (CLI)
  tests/test_main.cpp   assert-based test runner used by CTest
```
