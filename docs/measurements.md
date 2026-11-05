# Measurements

Numbers from `make bench` (`sim/bench.py`), which starts its own backend on free ports with a scratch database and drives it with the real agent binary. Taken 2026-11-05 on an Apple M3 Max laptop in ordinary use, not an idle machine: one run, ten-second windows. Treat them as what this build does on a laptop, not as limits.

A telemetry frame is 88 bytes: 36-byte header, 48-byte payload, 4-byte CRC-32. At 10 Hz that is 880 bytes per second per device before UDP/IP overhead.

## Ingest throughput

All agents and the backend on one machine over loopback. The backend is one Python process: asyncio UDP receive, decode and CRC check, rule evaluation per frame, batched SQLite inserts.

| agents x rate | offered frames/s | ingested frames/s | loss (mean / worst device) | backend CPU |
|---|---|---|---|---|
| 6 x 10 Hz | 60 | 61 | 0.00 % / 0.00 % | 3 % of one core |
| 20 x 50 Hz | 1,000 | 1,004 | 0.00 % / 0.00 % | 23 % of one core |
| 40 x 100 Hz | 4,000 | 4,007 | 0.00 % / 0.00 % | 60 % of one core |
| 50 x 200 Hz | 10,000 | 10,013 | 0.00 % / 0.00 % | 77 % of one core |
| 50 x 400 Hz | 20,000 | 11,555 | 40.27 % / 55.46 % | 98 % of one core |

The demo fleet (first row) uses 3 % of a core. The process ingests 10,000 frames per second without losing one and saturates a single core somewhere between 10,000 and 20,000; past that the kernel drops datagrams at the socket buffer, and the loss shows up in the sequence-gap measurement, which is what it is for. Loss here is computed by the backend from sequence numbers, not from the offered rate, so it does not depend on how accurately the agents hold their rate.

## Latency

Three devices at 10 Hz.

| path | samples | median | p95 | max |
|---|---|---|---|---|
| device timestamp -> backend receive | 300 | 0.7 ms | 1.2 ms | 2.3 ms |
| POST /fault -> flag alert opened | 24 of 24 | 91 ms | 100 ms | 101 ms |

The first row is the frame header's timestamp against the backend's clock at receive. Both are the same clock here; across machines it includes their clock offset. It is exposed per device as `stats.transit_ms`.

The second row is the whole control loop: REST call, `FAULT_INJECT` over TCP, the device sets the flag, the next telemetry frame carries it over UDP, the rule engine opens the alert. It is bounded by the 100 ms telemetry period, not by the backend.

## Packet loss: injected vs measured

One device at 50 Hz through `lossy_proxy.py`. Each drop rate held until the backend's 200-frame window had refilled, then the device's `loss_pct` sampled 20 times over 10 s.

| drop rate set on the proxy | proxy actually dropped | backend measured from sequence gaps |
|---|---|---|
| 0 % | 0.0 % | 0.0 % |
| 5 % | 5.3 % | 4.8 % |
| 10 % | 11.1 % | 10.2 % |
| 20 % | 20.9 % | 22.0 % |
| 30 % | 31.2 % | 31.6 % |
| 50 % | 49.7 % | 49.1 % |

The backend never sees the proxy's counters. It infers loss from gaps in the sequence numbers of the frames that did arrive, and lands within about one point of what the proxy dropped. Individual readings move by 3 to 8 points around the mean, because the window is only the last 200 received frames.
