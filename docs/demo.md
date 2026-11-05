# Demo walkthrough

How to show this project in about five minutes, what to say while it runs, and the questions it tends to raise. Everything here runs on one laptop; the Jetson is optional.

## Before you start

```bash
make demo-check     # the same story once, as a test: expect "21/21 expectations passed" (3 minutes)
make demo           # the real thing; prints the dashboard URL, stays up until Ctrl-C
```

Run `make demo-check` some time before, not in front of people. Then start `make demo`, open the URL it prints, and put the terminal and the browser side by side: the terminal narrates each step (`>>> 2/6 ...`) and prints `PASS` as each claim is checked.

If ports 8780-8782 are taken, the demo says so and moves to 8790-8792. Nothing else needs to be running, and nothing needs the network.

If it cannot be run live, `docs/img/demo.gif` is the same story recorded from a real run.

## Thirty seconds on what it is

> It is a small connected-vehicle telemetry stack, end to end. A C++ agent behaves like a motor-controller ECU and streams a binary protocol I defined: telemetry over UDP, control over TCP. A Python backend ingests it, stores it, raises alerts, and can push config and firmware back down. A test harness runs the real binaries through fault scenarios and checks what the backend reports. The demo you are about to see is one of those scenarios, so every step is asserted, not just shown.

## The story

`make demo` starts a backend on an empty database, a lossy UDP proxy, and six agent processes. Times are seconds from the moment the agents start.

| When | What happens | Where to look | What to say |
|---|---|---|---|
| 0-15 | Six ECUs connect and stream at 10 Hz (one at 5 Hz). | Fleet page: six green tiles, `DEVICES 6/6`, loss 0 %. | Each tile is a separate process running the real agent. They send HELLO on TCP and telemetry on UDP. |
| 15 | An `OVERTEMP` fault is injected into ECU-0001 for 25 s through the REST API. | ECU-0001 turns red, `FAULT`. Click it: temperature climbing, alerts opening one after another. | The command went REST, then TCP to the device. The device set the flag, the next telemetry frame carried it, and the rule engine opened the alert. That loop measures 91 ms. |
| 32 | The backend diagnoses ECU-0001. | `Diagnose` tab on the device page. | It builds a context pack from recent stats, alerts, logs and config. With an API key a model answers in a fixed JSON shape; without one, a rule-based fallback does, and the result says which. |
| 45 | A config push lowers ECU-0004's temperature limit to 30 °C. At 70 it is restored. | Fleet page: ECU-0004 goes to `FAULT` with nobody injecting anything. `Config` tab shows v2 acked, then v3. | Config is versioned and acked. The device enforces its own limit, so the alert comes from the device's protection, not from the backend guessing. |
| 75-105 | Firmware 1.1.0 is registered and deployed to ECU-0002, then ECU-0003. | Their tiles change from `fw 1.0.0` to `fw 1.1.0`. ECU-0006 stays on 1.0.0. | Each device acks, simulates the download, drops its connection, "reboots", reconnects and says HELLO with the new version. The image transfer is simulated; the protocol exchange is real. |
| 110 | The proxy starts dropping 30 % of ECU-0005's packets. At 138 it stops. | ECU-0005's tile: `LOSS` climbing past 20 %, then a `packet_loss` alert. | UDP does not report loss. Every frame has a sequence number, and the backend infers loss from the gaps. Measured against the proxy's own counters it is within about a point. |
| 138-180 | Everything recovers. | Alerts drawer empties. Terminal: `21/21 expectations passed`. | Alerts close on their own once the condition has been clear for 10 s. Nothing was acknowledged by hand. |

After the story the fleet stays up. Good things to click:

- **Fault injection** tab on any device: set `ENCODER_FAULT` or `VIBRATION_HIGH` and watch the alert open.
- **Config** tab: push a new telemetry rate and watch the ack.
- Range buttons `5M / 1H / 24H / 7D`: the long ranges read 1-minute rollups, which are kept after raw samples are pruned.
- `http://localhost:<port>/docs`: the REST API the demo is driven through.

## With the Jetson

The story does not depend on the board. The demo's backend listens for devices on all interfaces, so the Jetson's agent can join as a seventh device when the demo is on the default ports (8780-8782, which means stopping any other backend first). That has been run against the long-running backend, where the real thermal event in [hardware.md](hardware.md) was recorded, but not yet against `make demo`.

## Questions this raises

**Why UDP for telemetry and TCP for control?**
Telemetry is high-rate and each sample is replaced by the next one a tenth of a second later, so retransmitting a lost sample is worth less than sending the next. Control messages are rare and each one matters: a config push or a firmware command has to arrive, in order, exactly once, and be acknowledged. So telemetry is fire-and-forget on UDP and control is a TCP connection the device opens and keeps, which also works through NAT because the device dials out.

**If UDP does not tell you about loss, how do you know?**
Every frame carries a per-device sequence number. The backend keeps the last 200 received frames per device and computes loss from the gaps. `make bench` checks this against a proxy that drops a known fraction: from 5 % to 50 % the inferred loss is within about one point of what was dropped ([measurements.md](measurements.md)). A sequence jump of more than 10,000 is treated as a device restart, not loss.

**UDP already has a checksum. Why a CRC-32?**
One frame format serves both transports. On TCP there are no message boundaries, so the stream decoder needs magic, length and CRC to find frames and to resynchronise after garbage. The same framing would carry over to a serial link or CAN-TP on a microcontroller, where there is no UDP checksum at all. Bad frames are counted per device, which is itself a health signal.

**Why SQLite?**
One process, one writer, and nothing to deploy. Inserts are batched every half second in WAL mode, and a single Python process ingests 10,000 frames per second with no loss on one core. Raw samples are kept 24 hours; 1-minute rollups are kept and serve the long chart ranges. It stops being the right choice when there is more than one ingest process or the data outgrows one disk: then the ingest path would write to a queue and a time-series store, and the rule engine would move out of the receive loop.

**What breaks first if the fleet grows?**
The receive loop. It is one Python core doing decode, CRC, rule evaluation and buffering per frame; it saturates between 10,000 and 20,000 frames per second, and past that the kernel drops datagrams at the socket. The sequence-gap measurement reports exactly that as loss, so the system at least says when it is overloaded.

**Thresholds or anomaly detection?**
Both, for different failures. Thresholds and device-reported fault flags are immediate and explainable. The statistical rule (EWMA mean and variance per device and metric, z-score above 4) catches a change that stays inside the limits. It was also the noisiest part: in one demo run 22 of 32 alerts were anomaly warnings on healthy devices, from road bumps and from ordinary acceleration. Three changes fixed it. The hold is now three seconds, not three samples, so it does not depend on telemetry rate. Current, voltage and vibration are not judged while rpm is changing, because they follow what the driver asks for. And anomalous samples no longer feed the variance: writing the tests for the first two showed that a sustained deviation was inflating its own variance and dropping out after about a second.

**What did running on real hardware change?**
Two things, both from the Jetson. A board sitting exactly at its temperature limit produced 13 fault set/clear pairs in a minute, so device-side thresholds got hysteresis. And the backend's fixed 80 V undervoltage rule was permanently true for a 5 V board, so threshold rules got per-device overrides. Neither showed up in simulation, because the simulated plant never sat on a limit and was always a 96 V pack.

**What does testing with the real binaries buy over mocks?**
The scenarios start the actual agent and the actual backend and talk to them only through the public API, so they test the seams: the protocol in two languages, timing, reconnects, the alert lifecycle. They found things unit tests did not: a scenario check that raced the fault it was checking, a runner that would silently attach to an already-running backend and its old data, and the anomaly noise above. The cost is time (the demo scenario takes three minutes) and fixed timing that has to leave margin for a slow CI runner.

**How do you know the C++ and Python codecs agree?**
One file of golden frames, `firmware/tests/fixtures/frames.txt`, holds one frame per message type as hex. The C++ test suite and the Python test suite both read it; each has to decode every frame, find the expected header fields, and re-encode it to exactly the same bytes. A field moved or mis-sized in either codec fails that codec's test. On top of that the integration test and every SIL run put a C++ agent's frames through the Python backend, and the bad-CRC counter has to stay at zero. The file exists because an earlier version of the README claimed shared vectors that were not there; writing the walkthrough is what caught it.

**What is simulated and what is real?**
Real: the agent binary, the wire protocol, both transports, ingestion, storage, the alert engine, config and OTA message exchanges, reconnect with jittered exponential backoff (1 s to 30 s), and on the Jetson the temperature, voltage and current readings. Simulated: the motor, battery and thermal model in the fleet devices; the firmware image (a version string and a delay, no bytes transferred, no signature, no rollback); and the network impairment, which is a proxy rather than a bad radio link.

**What about security?**
There is none, and it should be said plainly: no authentication on the REST API, no device identity beyond the ID in the frame, no encryption on either transport. For anything outside a lab the control channel would be mutual TLS with per-device certificates, telemetry would move to DTLS or inside the same session, firmware would be signed and verified on the device, and the API would sit behind real authentication.

**What would you do next?**
A real microcontroller port, since the agent is plain C++17 on POSIX sockets. CAN as an input, which is where an ECU's data actually comes from. Alert notifications. And moving rule evaluation off the receive path, which is the first scaling limit.
