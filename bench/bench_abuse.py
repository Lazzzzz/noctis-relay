"""Legitimate players under hostile load, with the default abuse limits.

    python3 bench/bench_abuse.py [--attack single|rotate|create|slow|none] [--seconds 10] [--check]

Starts a real relay with **default** limits, trusting 127.0.0.1 as its
reverse proxy so that X-Forwarded-For can emulate many client networks on
one machine. Legitimate players each have their own address, open a session
and heartbeat every ``--interval`` seconds; an attacker meanwhile:

- ``single``: floods ``GET /v1/info`` from one address over many connections;
- ``rotate``: same flood, a fresh random address on every request;
- ``create``: floods ``POST /v1/sessions`` with rotating addresses;
- ``slow``: opens many requests from one address and never sends the body.

Reports what the players experienced (statuses, latency) and what the
attacker obtained. Client and server share the machine. ``--warmup`` lets players open their
sessions before the attack starts; ``--check`` exits
with an error unless every heartbeat of an existing session got ``200``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
    if not values:
        return float("nan")
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


async def call(reader, writer, method, path, xff, body=None, token=None):
    data = b"" if body is None else json.dumps(body).encode()
    head = f"{method} {path} HTTP/1.1\r\nHost: bench\r\nX-Forwarded-For: {xff}\r\nContent-Length: {len(data)}\r\n"
    if body is not None:
        head += "Content-Type: application/json\r\n"
    if token:
        head += f"Authorization: Bearer {token}\r\n"
    writer.write(head.encode() + b"\r\n" + data)
    status_line = await reader.readline()
    if not status_line:
        raise ConnectionError("closed")
    length, close = 0, False
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b""):
            break
        low = line.lower()
        if low.startswith(b"content-length:"):
            length = int(line.split(b":")[1])
        elif low.startswith(b"connection:") and b"close" in low:
            close = True
    payload = await reader.readexactly(length) if length else b""
    return int(status_line.split()[1]), payload, close


def bump(table, key):
    table[key] = table.get(key, 0) + 1


async def player(i, port, deadline, interval, lat, statuses, rng):
    xff = f"10.{(i >> 8) & 255}.{i & 255}.7"
    await asyncio.sleep(rng.uniform(0, interval))
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    token = None
    while time.perf_counter() < deadline:
        t0 = time.perf_counter()
        try:
            if token is None:
                status, payload, close = await call(reader, writer, "POST", "/v1/sessions", xff, {"protocol": 1})
                if status == 201:
                    token = json.loads(payload)["token"]
                bump(statuses, f"create {status}")
            else:
                status, _, close = await call(reader, writer, "PUT", "/v1/session", xff, {}, token)
                lat.append(time.perf_counter() - t0)
                bump(statuses, f"heartbeat {status}")
                if status == 401:
                    token = None
        except (ConnectionError, asyncio.IncompleteReadError):
            bump(statuses, "connection lost")
            close = True
        if close:
            writer.close()
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.sleep(interval)
    if token:
        try:
            await call(reader, writer, "DELETE", "/v1/session", xff, None, token)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
    writer.close()


async def flooder(port, deadline, mode, statuses, rng, warmup):
    await asyncio.sleep(warmup)
    reader = writer = None
    while time.perf_counter() < deadline:
        try:
            if writer is None:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
            xff = "198.51.100.66" if mode == "single" else f"100.{rng.randrange(64, 128)}.{rng.randrange(256)}.{rng.randrange(1, 255)}"
            if mode == "create":
                status, _, close = await call(reader, writer, "POST", "/v1/sessions", xff, {"protocol": 1})
            else:
                status, _, close = await call(reader, writer, "GET", "/v1/info", xff)
            bump(statuses, status)
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            bump(statuses, "reset")
            close = True
        if close and writer is not None:
            writer.close()
            writer = None


async def slow_body(port, deadline, statuses, warmup):
    await asyncio.sleep(warmup)
    while time.perf_counter() < deadline:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"POST /v1/sessions HTTP/1.1\r\nX-Forwarded-For: 198.51.100.66\r\n"
                         b"Content-Type: application/json\r\nContent-Length: 14\r\n\r\n{")
            line = await reader.readline()  # an answer, or EOF at body timeout
            bump(statuses, int(line.split()[1]) if line else "cut at body timeout")
            writer.close()
        except (ConnectionError, OSError):
            bump(statuses, "reset")
            await asyncio.sleep(0.05)


def rss_mb(pid):
    # Linux containers need no procps package just to measure their RSS.
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError):
        pass
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                             capture_output=True, text=True).stdout.strip()
        return int(out) / 1024 if out else float("nan")
    except (OSError, ValueError):
        return float("nan")


async def main_async(args):
    port = free_port()
    env = {
        **os.environ,
        "PYTHONPATH": ROOT,
        "NOCTIS_OPERATOR_NAME": "bench",
        "NOCTIS_OPERATOR_CONTACT": "bench@example.invalid",
        "NOCTIS_PORT": str(port),
        # Only change from the defaults: 127.0.0.1 plays the reverse proxy.
        "NOCTIS_TRUST_PROXY": "1",
        "NOCTIS_TRUSTED_PROXIES": "127.0.0.1/32",
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
                    raise SystemExit("relay exited during startup")
                await asyncio.sleep(0.05)
        rng = random.Random(args.seed)
        lat, legit, attack = [], {}, {}
        start = time.perf_counter()
        deadline = start + args.seconds
        tasks = [player(i, port, deadline, args.interval, lat, legit, rng) for i in range(args.players)]
        if args.attack == "slow":
            tasks += [slow_body(port, deadline, attack, args.warmup) for _ in range(args.attackers)]
        elif args.attack != "none":
            tasks += [flooder(port, deadline, args.attack, attack, rng, args.warmup) for _ in range(args.attackers)]
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start
        print(f"attack={args.attack}  {args.attackers} attacker connections, {args.players} players "
              f"heartbeating every {args.interval}s, {elapsed:.1f}s, "
              f"attack after {args.warmup}s, default limits")
        print(f"players : {dict(sorted(legit.items()))}")
        ms = [v * 1000 for v in lat]
        print(f"  heartbeat p50 {pct(ms, 50):.2f} ms  p95 {pct(ms, 95):.2f} ms  p99 {pct(ms, 99):.2f} ms")
        total = sum(v for v in attack.values())
        print(f"attacker: {total / elapsed:,.0f} attempts/s  {dict(sorted(attack.items(), key=str))}")
        print(f"server RSS after run: {rss_mb(proc.pid):.1f} MiB")
        bad = {k: v for k, v in legit.items() if not k.startswith("create") and k != "heartbeat 200"}
        if args.check and (bad or not legit.get("heartbeat 200")):
            raise SystemExit(f"players were degraded: {bad or 'no heartbeat'}")
    finally:
        proc.terminate()
        proc.wait(5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--attack", choices=["none", "single", "rotate", "create", "slow"], default="rotate")
    ap.add_argument("--attackers", type=int, default=64, help="concurrent attacker connections")
    ap.add_argument("--players", type=int, default=200)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--warmup", type=float, default=0.0, help="seconds before the attack starts")
    ap.add_argument("--check", action="store_true", help="fail unless every heartbeat got 200")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
