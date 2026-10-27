# Real hardware target: Jetson Orin Nano Super

First real device for the stack (2026-10-27). The backend stays on the Mac; the board runs the agent and reaches the Mac over Tailscale (ingest UDP :8781, TCP :8782 at the Mac's tailnet address, bound `0.0.0.0` by `ECU_BIND`'s default).

## Build on the board

Ubuntu 24.04 arm64, gcc 13.3, cmake 3.28. Native build takes ~5 s, CTest 15/15 in ~1 s. No code changes were needed for aarch64.

```
make jetson-deploy      # rsync firmware/ docs/ deploy/ → build → ctest → restart service
make jetson-logs        # journalctl -f for the agent
```

`JETSON=<ssh alias>` overrides the host (default `jetson`, see the `jetson` repo for the aliases).

## Run at boot

`deploy/jetson/install.sh` (run once on the board) installs `deploy/jetson/ecu-agent.service` as a **user** service enabled for `default.target`; settings live in `~/ecu-telemetry/agent.env` (device id, backend address, hw/fw strings, profile). `Restart=always` so a crash or a backend outage just reconnects (the agent already backs off on its own).

Device ids are at most 12 characters: `ECU-JETSON-1`.

## First result

`ECU-JETSON-1` online in the dashboard at 10 Hz, `loss_pct` 0.0 over Wi-Fi + Tailscale (direct path, no relay), TCP control channel connected on the first try.

## Next on this board

See `GOALS.md` → "Jetson week": real sensors (`/sys/class/thermal`, INA3221), SocketCAN source, OTA to the real service, SIL with a mixed fleet, arm64 container image.
