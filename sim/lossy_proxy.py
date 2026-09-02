#!/usr/bin/env python3
"""UDP chaos proxy for the ECU telemetry SIL environment.

Sits between ECU agents and the backend UDP ingest port and applies
configurable network impairments to every datagram flowing device -> backend:

  --drop       probability of silently discarding a datagram
  --delay-ms   fixed one-way latency added to each datagram
  --jitter-ms  uniform random extra latency in [0, jitter]
  --reorder    probability that a datagram is held back an extra ~2x delay
               so it arrives after its successors
  --dup        probability that a datagram is delivered twice

A tiny HTTP control endpoint (``--control-port``) accepts ``POST /set`` with a
JSON body like ``{"drop": 0.5, "delay_ms": 100}`` so scenarios can change the
impairment profile mid-run (the ``proxy_set`` timeline action in run_sil.py).
``GET /stats`` returns counters; ``GET /health`` returns ``{"status":"ok"}``.

Only the Python standard library is used.

Example:
    python sim/lossy_proxy.py --listen 9781 --forward 127.0.0.1:8781 \
        --drop 0.1 --delay-ms 50 --jitter-ms 20 --reorder 0.05 --control-port 9790
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import signal
import sys
import time

TUNABLES = ("drop", "delay_ms", "jitter_ms", "reorder", "dup")


class Impairments:
    def __init__(self, drop=0.0, delay_ms=0.0, jitter_ms=0.0, reorder=0.0, dup=0.0):
        self.drop = float(drop)
        self.delay_ms = float(delay_ms)
        self.jitter_ms = float(jitter_ms)
        self.reorder = float(reorder)
        self.dup = float(dup)

    def update(self, values: dict) -> dict:
        changed = {}
        for k, v in values.items():
            if k not in TUNABLES:
                raise ValueError(f"unknown tunable {k!r} (allowed: {', '.join(TUNABLES)})")
            v = float(v)
            if k in ("drop", "reorder", "dup") and not 0.0 <= v <= 1.0:
                raise ValueError(f"{k} must be in [0,1]")
            if v < 0:
                raise ValueError(f"{k} must be >= 0")
            setattr(self, k, v)
            changed[k] = v
        return changed

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in TUNABLES}


class Stats:
    def __init__(self):
        self.received = 0
        self.forwarded = 0
        self.dropped = 0
        self.duplicated = 0
        self.reordered = 0
        self.returned = 0  # backend -> device (unused by TELEMETRY but supported)
        self.started = time.time()

    def as_dict(self) -> dict:
        d = dict(vars(self))
        d["uptime_s"] = round(time.time() - self.started, 1)
        return d


class ProxyProtocol(asyncio.DatagramProtocol):
    """Listens on the public side; forwards to the backend, remembering the
    last client address per source so any return traffic can be routed back."""

    def __init__(self, forward: tuple[str, int], imp: Impairments, stats: Stats, rng: random.Random, verbose: bool):
        self.forward = forward
        self.imp = imp
        self.stats = stats
        self.rng = rng
        self.verbose = verbose
        self.transport: asyncio.DatagramTransport | None = None
        self.loop = asyncio.get_event_loop()
        # one upstream socket per client so the backend can distinguish sources
        self.upstreams: dict[tuple, asyncio.DatagramTransport] = {}

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        self.stats.received += 1
        if self.rng.random() < self.imp.drop:
            self.stats.dropped += 1
            if self.verbose:
                print(f"[proxy] drop {len(data)}B from {addr}", file=sys.stderr)
            return
        copies = 1
        if self.imp.dup and self.rng.random() < self.imp.dup:
            copies = 2
            self.stats.duplicated += 1
        for _ in range(copies):
            delay = self.imp.delay_ms + self.rng.uniform(0, self.imp.jitter_ms)
            if self.imp.reorder and self.rng.random() < self.imp.reorder:
                delay += max(2 * self.imp.delay_ms, 2 * self.imp.jitter_ms, 30.0)
                self.stats.reordered += 1
            if delay <= 0:
                self.loop.create_task(self._send(data, addr))
            else:
                self.loop.call_later(delay / 1000.0, lambda d=data, a=addr: self.loop.create_task(self._send(d, a)))

    async def _send(self, data: bytes, client_addr):
        up = self.upstreams.get(client_addr)
        if up is None or up.is_closing():
            up, _ = await self.loop.create_datagram_endpoint(
                lambda: ReturnProtocol(self, client_addr), remote_addr=self.forward
            )
            self.upstreams[client_addr] = up
        try:
            up.sendto(data)
            self.stats.forwarded += 1
        except OSError as e:
            if self.verbose:
                print(f"[proxy] send error: {e}", file=sys.stderr)

    def return_to_client(self, data: bytes, client_addr):
        if self.transport:
            self.transport.sendto(data, client_addr)
            self.stats.returned += 1

    def error_received(self, exc):
        if self.verbose:
            print(f"[proxy] error: {exc}", file=sys.stderr)

    def close(self):
        for up in self.upstreams.values():
            up.close()
        if self.transport:
            self.transport.close()


class ReturnProtocol(asyncio.DatagramProtocol):
    def __init__(self, proxy: ProxyProtocol, client_addr):
        self.proxy = proxy
        self.client_addr = client_addr

    def datagram_received(self, data, addr):
        self.proxy.return_to_client(data, self.client_addr)

    def error_received(self, exc):
        pass


# --------------------------------------------------------------------------- #
# Minimal HTTP control server (asyncio streams, no framework)
# --------------------------------------------------------------------------- #
async def _http_response(writer: asyncio.StreamWriter, status: int, body: dict):
    payload = json.dumps(body).encode()
    reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed"}.get(status, "OK")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    writer.write(head + payload)
    await writer.drain()
    writer.close()


def make_control_handler(imp: Impairments, stats: Stats):
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(reader.readline(), 5)
            parts = request_line.decode(errors="replace").split()
            if len(parts) < 2:
                return await _http_response(writer, 400, {"error": "bad request line"})
            method, path = parts[0].upper(), parts[1]
            headers = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), 5)
                if line in (b"\r\n", b"\n", b""):
                    break
                k, _, v = line.decode(errors="replace").partition(":")
                headers[k.strip().lower()] = v.strip()
            body = b""
            n = int(headers.get("content-length", "0") or 0)
            if n:
                body = await asyncio.wait_for(reader.readexactly(n), 5)

            if path == "/health":
                return await _http_response(writer, 200, {"status": "ok"})
            if path == "/stats":
                return await _http_response(writer, 200, {"stats": stats.as_dict(), "impairments": imp.as_dict()})
            if path == "/set":
                if method != "POST":
                    return await _http_response(writer, 405, {"error": "use POST"})
                try:
                    values = json.loads(body or b"{}")
                    changed = imp.update(values)
                except (ValueError, TypeError) as e:
                    return await _http_response(writer, 400, {"error": str(e)})
                print(f"[proxy] impairments updated: {changed}", file=sys.stderr)
                return await _http_response(writer, 200, {"ok": True, "impairments": imp.as_dict()})
            return await _http_response(writer, 404, {"error": "not found"})
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            writer.close()

    return handler


def parse_hostport(s: str) -> tuple[str, int]:
    host, _, port = s.rpartition(":")
    if not host:
        host = "127.0.0.1"
    return host, int(port)


async def amain(args) -> int:
    imp = Impairments(args.drop, args.delay_ms, args.jitter_ms, args.reorder, args.dup)
    stats = Stats()
    rng = random.Random(args.seed)
    loop = asyncio.get_running_loop()
    forward = parse_hostport(args.forward)

    transport, proto = await loop.create_datagram_endpoint(
        lambda: ProxyProtocol(forward, imp, stats, rng, args.verbose),
        local_addr=(args.bind, args.listen),
    )
    control = None
    if args.control_port:
        control = await asyncio.start_server(make_control_handler(imp, stats), args.bind, args.control_port)

    # Optional transparent TCP relay (no impairments) so an agent can point its single --host at
    # the proxy for both transports; the control channel is passed through untouched.
    tcp_relay = None
    if args.tcp_listen:
        tcp_target = parse_hostport(args.tcp_forward or f"{forward[0]}:8782")

        async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                ur, uw = await asyncio.open_connection(*tcp_target)
            except OSError as e:
                if args.verbose:
                    print(f"[proxy] tcp connect to {tcp_target} failed: {e}", file=sys.stderr)
                writer.close()
                return

            async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter):
                try:
                    while True:
                        chunk = await src.read(65536)
                        if not chunk:
                            break
                        dst.write(chunk)
                        await dst.drain()
                except (ConnectionError, asyncio.CancelledError):
                    pass
                finally:
                    try:
                        dst.close()
                    except Exception:
                        pass

            await asyncio.gather(pipe(reader, uw), pipe(ur, writer))

        tcp_relay = await asyncio.start_server(relay, args.bind, args.tcp_listen)
        print(f"[proxy] tcp {args.bind}:{args.tcp_listen} -> {tcp_target[0]}:{tcp_target[1]} (pass-through)", file=sys.stderr)
    print(
        f"[proxy] udp {args.bind}:{args.listen} -> {forward[0]}:{forward[1]}  "
        f"impairments={imp.as_dict()}  control={'http://%s:%d' % (args.bind, args.control_port) if control else 'off'}",
        file=sys.stderr,
        flush=True,
    )

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    async def stats_printer():
        while not stop.is_set():
            await asyncio.sleep(args.stats_every)
            print(f"[proxy] stats {stats.as_dict()}", file=sys.stderr, flush=True)

    tasks = []
    if args.stats_every > 0:
        tasks.append(asyncio.create_task(stats_printer()))
    if args.duration > 0:
        loop.call_later(args.duration, stop.set)
    await stop.wait()
    for t in tasks:
        t.cancel()
    proto.close()
    for srv in (control, tcp_relay):
        if srv:
            srv.close()
            await srv.wait_closed()
    print(f"[proxy] final stats {json.dumps(stats.as_dict())}", file=sys.stderr, flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--listen", type=int, default=9781, help="UDP port to listen on (devices point here)")
    p.add_argument("--bind", default="0.0.0.0", help="address to bind listen + control ports")
    p.add_argument("--forward", default="127.0.0.1:8781", help="backend UDP host:port")
    p.add_argument("--drop", type=float, default=0.0)
    p.add_argument("--delay-ms", type=float, default=0.0)
    p.add_argument("--jitter-ms", type=float, default=0.0)
    p.add_argument("--reorder", type=float, default=0.0)
    p.add_argument("--dup", type=float, default=0.0)
    p.add_argument("--control-port", type=int, default=0, help="HTTP control port (0 = disabled)")
    p.add_argument("--tcp-listen", type=int, default=0, help="also relay TCP on this port, unimpaired (0 = off)")
    p.add_argument("--tcp-forward", default="", help="TCP relay target host:port (default: forward host, port 8782)")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--duration", type=float, default=0, help="seconds to run then exit (0 = forever)")
    p.add_argument("--stats-every", type=float, default=0, help="print stats every N s (0 = off)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    for name in ("drop", "reorder", "dup"):
        if not 0.0 <= getattr(args, name) <= 1.0:
            print(f"--{name} must be in [0,1]", file=sys.stderr)
            return 2
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
