#!/usr/bin/env python3
"""Measure the numbers the README quotes, on this machine, with the real binaries.

Starts its own backend on free ports with a scratch database, then:

  throughput   N agents x HZ for a few seconds per level: frames ingested per second, loss from
               sequence gaps, backend CPU. Levels climb until loss shows.
  latency      one-way: device timestamp in the frame header -> backend receive time (same clock);
               fault -> alert: POST /fault until the flag alert's opened_at.
  loss         one device through lossy_proxy at several drop rates: loss the backend measures
               from sequence gaps vs the rate the proxy applied vs what the proxy counted.

    python sim/bench.py                 # everything, about 4 minutes, prints markdown tables
    python sim/bench.py --only loss

Dependencies: the same as run_sil.py (pyyaml, httpx).
"""
from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_sil as R  # noqa: E402

FLAGS = list(R.FAULT_NAMES)
LEVELS = [(6, 10), (20, 50), (40, 100), (50, 200), (50, 400)]   # (agents, hz)
DROPS = [0.0, 0.05, 0.10, 0.20, 0.30, 0.50]


def cpu_seconds(pid: int) -> float:
    """CPU time a process has used so far (user+system), from ps."""
    out = subprocess.run(["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    parts = [float(x) for x in out.replace("-", ":").split(":")] if out else [0.0]
    return sum(v * 60 ** i for i, v in enumerate(reversed(parts)))


class Bench:
    def __init__(self, args):
        base = next(b for b in range(8890, 8990, 10) if R.ports_free(b))
        self.sc = {"name": "bench", "duration_s": 3600, "devices": [],
                   "backend": {"http": f"http://127.0.0.1:{base}", "udp_port": base + 1, "tcp_port": base + 2},
                   "proxy": {"listen": base + 5, "control_port": base + 6, "drop": 0.0}}
        self.args = argparse.Namespace(agent=args.agent, backend_timeout=30.0, via_proxy=[], hold=True, proxy_port=base + 5)
        self.runner = R.Runner(self.sc, self.args)
        self.api = R.Api(self.sc["backend"]["http"])
        self.runner.t0 = time.time()
        self.runner.start_backend(self.api)
        self.backend_pid = self.runner.children[0].proc.pid

    def agents(self, n: int, hz: int, prefix: str, via_proxy: bool = False) -> list[R.Child]:
        out = []
        for i in range(n):
            d = {"id": f"{prefix}-{i + 1:03d}", "hz": hz, "seed": 500 + i, "profile": ("city", "highway", "idle")[i % 3],
                 "via_proxy": via_proxy, "log_level": "warn"}
            out.append(self.runner.spawn(f"agent:{d['id']}", self.runner.agent_cmd(d)))
        return out

    def stop(self, children: list[R.Child]):
        for ch in children:
            ch.proc.terminate()
        for ch in children:
            ch.proc.wait(timeout=5)
            self.runner.children.remove(ch)

    def fleet(self) -> dict:
        return self.api.get("/api/stats")

    # ------------------------------------------------------------------ throughput
    def throughput(self, window_s: float = 10.0) -> list[dict]:
        rows = []
        for n, hz in LEVELS:
            prefix = f"T{hz}"
            ch = self.agents(n, hz, prefix)
            time.sleep(4.0)                      # connect, HELLO, and fill the 200-frame loss windows
            a, cpu_a, t_a = self.fleet(), cpu_seconds(self.backend_pid), time.time()
            time.sleep(window_s)
            b, cpu_b, t_b = self.fleet(), cpu_seconds(self.backend_pid), time.time()
            devs = [d for d in self.api.get("/api/devices") if d["device_id"].startswith(prefix)]
            loss = statistics.mean(d["loss_pct"] for d in devs)
            rows.append({"agents": n, "hz": hz, "offered": n * hz,
                         "ingested": (b["frames_total_rx"] - a["frames_total_rx"]) / (t_b - t_a),
                         "loss_pct": loss, "worst_pct": max(d["loss_pct"] for d in devs),
                         "cpu_pct": 100 * (cpu_b - cpu_a) / (t_b - t_a)})
            self.stop(ch)
            time.sleep(1.0)
            print(f"  {n:>3} x {hz:>3} Hz  offered {n * hz:>6}/s  ingested {rows[-1]['ingested']:>8.0f}/s  "
                  f"loss {loss:5.2f}%  cpu {rows[-1]['cpu_pct']:5.1f}%", file=sys.stderr)
            if loss > 5.0:
                break
        return rows

    # ------------------------------------------------------------------ latency
    def latency(self) -> dict:
        ch = self.agents(3, 10, "L")
        time.sleep(4.0)
        ids = [f"L-{i + 1:03d}" for i in range(3)]
        one_way = []
        for _ in range(100):                      # device clock -> backend receive, read from the latest frame
            for did in ids:
                ms = ((self.api.device(did) or {}).get("stats") or {}).get("transit_ms")
                if ms is not None:
                    one_way.append(ms)
            time.sleep(0.03)
        fault = []
        for flag in FLAGS:                        # a different flag each time, so no waiting for alerts to resolve
            for did in ids:
                t0 = time.time()
                self.api.post(f"/api/devices/{did}/fault", {"set": [flag], "clear": [], "duration_ms": 1500})
                deadline = t0 + 3.0
                while time.time() < deadline:
                    hit = [a for a in self.api.alerts(did, status="open") if a["rule"] == f"flag:{flag}"]
                    if hit:
                        fault.append((hit[0]["opened_at"] - t0) * 1000.0)
                        break
                    time.sleep(0.01)
            time.sleep(0.4)
        self.stop(ch)
        return {"one_way_ms": one_way, "fault_to_alert_ms": fault, "fault_attempts": len(FLAGS) * len(ids)}

    # ------------------------------------------------------------------ loss
    def loss(self, hz: int = 50) -> list[dict]:
        import httpx
        self.sc["devices"] = [{"id": "P-001", "via_proxy": True}]
        ctl = self.runner.start_proxy()
        ch = self.agents(1, hz, "P", via_proxy=True)
        time.sleep(4.0)
        rows = []
        for drop in DROPS:
            httpx.post(f"{ctl}/set", json={"drop": drop}, timeout=5).raise_for_status()
            time.sleep(200 / (hz * (1 - drop)) + 3.0)      # let the 200-frame window fill at this rate
            s0 = httpx.get(f"{ctl}/stats", timeout=5).json()["stats"]
            samples = []
            for _ in range(20):
                samples.append(self.api.device("P-001")["loss_pct"])
                time.sleep(0.5)
            s1 = httpx.get(f"{ctl}/stats", timeout=5).json()["stats"]
            rx, dropped = s1["received"] - s0["received"], s1["dropped"] - s0["dropped"]
            rows.append({"injected": 100 * drop, "proxy": 100 * dropped / rx if rx else 0.0,
                         "measured": statistics.mean(samples), "spread": max(samples) - min(samples)})
            print(f"  drop {100 * drop:4.0f}%  proxy dropped {rows[-1]['proxy']:5.1f}%  backend measured "
                  f"{rows[-1]['measured']:5.1f}% (spread {rows[-1]['spread']:.1f})", file=sys.stderr)
        self.stop(ch)
        return rows

    def close(self):
        self.runner.kill_all()
        self.api.close()


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=["throughput", "latency", "loss"])
    ap.add_argument("--agent", default=str(R.DEFAULT_AGENT))
    args = ap.parse_args()
    R.C.enabled = False
    b = Bench(args)
    try:
        if args.only in (None, "throughput"):
            print("throughput", file=sys.stderr)
            rows = b.throughput()
            print("\n| agents x rate | offered frames/s | ingested frames/s | loss (mean / worst device) | backend CPU |")
            print("|---|---|---|---|---|")
            for r in rows:
                print(f"| {r['agents']} x {r['hz']} Hz | {r['offered']:,} | {r['ingested']:,.0f} | "
                      f"{r['loss_pct']:.2f} % / {r['worst_pct']:.2f} % | {r['cpu_pct']:.0f} % of one core |")
        if args.only in (None, "latency"):
            print("latency", file=sys.stderr)
            lat = b.latency()
            ow, fa = lat["one_way_ms"], lat["fault_to_alert_ms"]
            print("\n| path | samples | median | p95 | max |")
            print("|---|---|---|---|---|")
            print(f"| device timestamp -> backend receive | {len(ow)} | {statistics.median(ow):.1f} ms | {pct(ow, 95):.1f} ms | {max(ow):.1f} ms |")
            print(f"| POST /fault -> flag alert opened (10 Hz telemetry) | {len(fa)} of {lat['fault_attempts']} | "
                  f"{statistics.median(fa):.0f} ms | {pct(fa, 95):.0f} ms | {max(fa):.0f} ms |")
        if args.only in (None, "loss"):
            print("loss", file=sys.stderr)
            rows = b.loss()
            print("\n| drop rate set on the proxy | proxy actually dropped | backend measured from sequence gaps |")
            print("|---|---|---|")
            for r in rows:
                print(f"| {r['injected']:.0f} % | {r['proxy']:.1f} % | {r['measured']:.1f} % |")
    finally:
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
