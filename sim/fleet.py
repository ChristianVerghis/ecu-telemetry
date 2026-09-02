#!/usr/bin/env python3
"""Launch N ECU agents against a running backend for manual dashboard demos.

    python sim/fleet.py --n 8 --profiles city,highway --drop 0.02
    python sim/fleet.py --n 4 --fault OVERTEMP@30:20 --fault-every 2   # every 2nd device gets the fault

Devices are named ECU-0001..ECU-000N, profiles are assigned round-robin, seeds
are deterministic. Ctrl-C (or SIGTERM) shuts every agent down cleanly.
Only the Python standard library is needed.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AGENT = REPO_ROOT / "firmware" / "build" / "ecu_agent"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n", type=int, default=4, help="number of agents")
    p.add_argument("--profiles", default="city,highway,idle", help="comma list, assigned round-robin")
    p.add_argument("--drop", type=float, default=0.0, help="agent-side UDP drop rate for every device")
    p.add_argument("--hz", type=int, default=10)
    p.add_argument("--fw", default="1.0.0")
    p.add_argument("--hw", default="SIM-MOTOR-A")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--udp-port", type=int, default=int(os.environ.get("ECU_UDP_PORT", 8781)))
    p.add_argument("--tcp-port", type=int, default=int(os.environ.get("ECU_TCP_PORT", 8782)))
    p.add_argument("--prefix", default="ECU-", help="device id prefix (ids are prefix + 4-digit index)")
    p.add_argument("--start-index", type=int, default=1)
    p.add_argument("--seed", type=int, default=1000, help="base seed; device i gets seed+i")
    p.add_argument("--fault", action="append", default=[], help="FLAG@START:DUR, applied to every --fault-every'th device")
    p.add_argument("--fault-every", type=int, default=1, help="apply --fault to every k-th device (1 = all)")
    p.add_argument("--duration", type=int, default=0, help="seconds each agent runs (0 = until Ctrl-C)")
    p.add_argument("--agent", default=str(DEFAULT_AGENT))
    p.add_argument("--quiet", action="store_true", help="discard agent stderr")
    args = p.parse_args(argv)

    agent = Path(args.agent)
    if not agent.exists():
        print(f"agent binary not found: {agent} (run `make build` or pass --agent)", file=sys.stderr)
        return 2
    if not 0.0 <= args.drop <= 1.0:
        print("--drop must be in [0,1]", file=sys.stderr)
        return 2
    profiles = [s.strip() for s in args.profiles.split(",") if s.strip()]
    if not profiles:
        print("--profiles must list at least one profile", file=sys.stderr)
        return 2

    procs: list[subprocess.Popen] = []
    sink = subprocess.DEVNULL if args.quiet else None
    for i in range(args.n):
        idx = args.start_index + i
        did = f"{args.prefix}{idx:04d}"
        if len(did) > 12:
            print(f"device id {did} exceeds 12 chars", file=sys.stderr)
            break
        cmd = [str(agent), "--id", did, "--host", args.host, "--udp-port", str(args.udp_port),
               "--tcp-port", str(args.tcp_port), "--hz", str(args.hz), "--fw", args.fw, "--hw", args.hw,
               "--profile", profiles[i % len(profiles)], "--drop-rate", str(args.drop),
               "--seed", str(args.seed + i), "--duration", str(args.duration)]
        if args.fault and i % max(1, args.fault_every) == 0:
            for f in args.fault:
                cmd += ["--fault", f]
        procs.append(subprocess.Popen(cmd, stdout=sink, stderr=sink, stdin=subprocess.DEVNULL, start_new_session=True))
        print(f"[fleet] {did:<10} profile={profiles[i % len(profiles)]:<8} pid={procs[-1].pid}"
              + (f" faults={args.fault}" if args.fault and i % max(1, args.fault_every) == 0 else ""), flush=True)

    def shutdown(*_):
        print("\n[fleet] shutting down", flush=True)
        for pr in procs:
            if pr.poll() is None:
                try:
                    os.killpg(pr.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.time() + 5
        for pr in procs:
            while pr.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            if pr.poll() is None:
                try:
                    os.killpg(pr.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    print(f"[fleet] {len(procs)} agents -> udp {args.host}:{args.udp_port} tcp {args.host}:{args.tcp_port}. Ctrl-C to stop.", flush=True)
    try:
        while any(pr.poll() is None for pr in procs):
            time.sleep(0.5)
        print("[fleet] all agents exited", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
