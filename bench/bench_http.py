"""End-to-end HTTP benchmark against a real relay process on localhost.

    python3 bench/bench_http.py [--clients 200] [--seconds 10]

Starts ``python -m noctis_relay`` in a subprocess (presence enabled, rate
limits raised for the benchmark only), opens one keep-alive connection per
simulated player and alternates presence updates and heartbeats as fast as
possible. Reports throughput, latency percentiles and the server's RSS.
Client and server share the machine, so numbers are a lower bound.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import socket
import subprocess
import sys
import time

ROOT = os.path.join(os.path.dirname(__file__), "..")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


async def call(reader, writer, method, path, body=None, token=None):
    data = b"" if body is None else json.dumps(body).encode()
    head = f"{method} {path} HTTP/1.1\r\nHost: bench\r\nContent-Length: {len(data)}\r\n"
    if body is not None:
        head += "Content-Type: application/json\r\n"
    if token:
        head += f"Authorization: Bearer {token}\r\n"
    writer.write(head.encode() + b"\r\n" + data)
    status_line = await reader.readline()
    length = 0
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b""):
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1])
    payload = await reader.readexactly(length) if length else b""
    return int(status_line.split()[1]), payload


async def player(i, port, deadline, hot, lat, statuses, rng, interval):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    status, payload = await call(reader, writer, "POST", "/v1/sessions", {"protocol": 1, "capabilities": ["presence"]})
    token = json.loads(payload)["token"]
    if i < hot:
        a, d = rng.uniform(0, 2 * math.pi), 64 * math.sqrt(rng.random())
        x, z = d * math.cos(a), d * math.sin(a)
    else:
        x, z = rng.uniform(-10_000, 10_000), rng.uniform(-10_000, 10_000)
    y = 70.0
    n = 0
    if interval:
        await asyncio.sleep(rng.uniform(0, interval))  # spread the clients
    while time.perf_counter() < deadline:
        if interval and n:
            await asyncio.sleep(interval)
        x += rng.uniform(-4, 4)
        z += rng.uniform(-4, 4)
        t0 = time.perf_counter()
        if n % 2 == 0:
            body = {"realm": "astralyth:noctis", "profile": "gen1-profile-a", "instance": "inst-%012d" % i,
                    "x": x, "y": y, "z": z}
            status, _ = await call(reader, writer, "PUT", "/v1/presence", body, token)
            lat["presence"].append(time.perf_counter() - t0)
        else:
            status, _ = await call(reader, writer, "PUT", "/v1/session", {}, token)
            lat["heartbeat"].append(time.perf_counter() - t0)
        statuses[status] = statuses.get(status, 0) + 1
        n += 1
    await call(reader, writer, "DELETE", "/v1/session", None, token)
    writer.close()


def rss_mb(pid):
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return int(out) / 1024 if out else float("nan")


async def main_async(args):
    port = free_port()
    env = {
        **os.environ,
        "PYTHONPATH": ROOT,
        "NOCTIS_OPERATOR_NAME": "bench",
        "NOCTIS_OPERATOR_CONTACT": "bench@example.invalid",
        "NOCTIS_PORT": str(port),
        "NOCTIS_PRESENCE": "1",
        "NOCTIS_PRESENCE_MIN_INTERVAL": "0",
        "NOCTIS_REQUEST_RATE": "10000",
        "NOCTIS_REQUEST_BURST": "100000",
        "NOCTIS_CREATE_RATE": "10000",
        "NOCTIS_CREATE_BURST": "100000",
        "NOCTIS_MAX_CONNECTIONS": str(args.clients + 10),
        "NOCTIS_MAX_REQUESTS_PER_CONNECTION": "10000",
    }
    proc = subprocess.Popen([sys.executable, "-m", "noctis_relay"], env=env, cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
                break
            except OSError:
                if proc.poll() is not None:
                    raise SystemExit("relay exited during startup (check configuration)")
                await asyncio.sleep(0.05)
        lat = {"presence": [], "heartbeat": []}
        statuses = {}
        rng = random.Random(args.seed)
        start = time.perf_counter()
        deadline = start + args.seconds
        # Note: all clients share one source IP, so the per-client rate limit
        # is raised above for the benchmark; production defaults stay strict.
        await asyncio.gather(*(player(i, port, deadline, args.hot, lat, statuses, rng, args.interval) for i in range(args.clients)))
        elapsed = time.perf_counter() - start
        total = sum(statuses.values())
        mode = f"paced every {args.interval}s" if args.interval else "closed loop, as fast as possible"
        print(f"{args.clients} keep-alive clients, {mode} ({args.hot} in a 64-block hot zone), {elapsed:.1f}s")
        print(f"throughput: {total / elapsed:,.0f} req/s  statuses: {dict(sorted(statuses.items()))}")
        for kind, values in lat.items():
            ms = [v * 1000 for v in values]
            print(f"  {kind:9s} n={len(ms):6d}  p50 {pct(ms, 50):6.2f} ms  p95 {pct(ms, 95):6.2f} ms  p99 {pct(ms, 99):6.2f} ms")
        print(f"server RSS after run: {rss_mb(proc.pid):.1f} MiB")
        # A real client heartbeats every 10 s: sustained throughput / 0.1 req/s
        # gives a rough ceiling on concurrent heartbeating players.
    finally:
        proc.terminate()
        proc.wait(5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clients", type=int, default=200)
    ap.add_argument("--hot", type=int, default=50)
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--interval", type=float, default=0.0, help="seconds between requests per client (0 = saturate)")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
