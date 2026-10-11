"""Real echo HTTP traffic under hostile load, without contacting a public relay.

    python3 bench/bench_echo_http.py --attack rotate --check
    python3 bench/bench_echo_http.py --attack invalid --check

Starts a child relay on loopback. Players use distinct simulated networks,
paired spatial cells and different game instances. Each publishes a full
movement/action clip, fetches nearby echoes and keeps its session alive.
Only echo/chat activation, their accelerated one-second pacing, and the
loopback proxy are changed from the operator defaults. All abuse budgets,
session/connection limits and cell caps stay enabled. The client and server
share the CPU, so this is a regression benchmark, not an Internet capacity
measurement. No game accounts or public services are involved.
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
import uuid

from bench_abuse import ROOT, bump, call, free_port, pct, rss_mb


def location(i):
    return {
        "realm": "astralyth:noctis", "profile": "bench-terrain-0123456789abcdef",
        "instance": f"bench-instance-{i:06d}",
        "x": (i // 2) * 128.0 + (i % 2) * 2.0, "y": 64.0, "z": 0.5,
    }


def clip(i):
    return {
        **location(i), "account": str(uuid.UUID(int=i + 1)),
        "frames": [[2 * k, 8 * k, 0, 0, 64, 0, 64, 1] for k in range(60)],
        "actions": [[4 * k, 2, 1 + k // 2, 0, 1, "minecraft:stone"] for k in range(8)],
        "chat": [[2, "il y a quelqu'un ?"]],
    }


async def close(writer):
    if writer is not None:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def player(i, port, deadline, statuses, latencies, counts):
    await asyncio.sleep(random.Random(i).uniform(0.05, 0.4))
    writer = None
    token = None
    xff = f"10.{(i >> 8) & 255}.{i & 255}.7"
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        status, body, cut = await call(reader, writer, "POST", "/v1/sessions", xff,
                                     {"protocol": 1, "capabilities": ["echoes", "chat"]})
        bump(statuses, f"create {status}")
        if status != 201 or cut:
            return
        token = json.loads(body)["token"]
        now = time.perf_counter()
        due = {"publish": now, "fetch": now + 0.5, "heartbeat": now + 1.0}
        routes = {
            "publish": ("PUT", "/v1/echoes/clip", clip(i), 1.05),
            "fetch": ("POST", "/v1/echoes/next", location(i), 1.05),
            "heartbeat": ("PUT", "/v1/session", {}, 2.0),
        }
        while time.perf_counter() < deadline:
            operation = min(due, key=due.get)
            await asyncio.sleep(max(0.0, due[operation] - time.perf_counter()))
            if time.perf_counter() >= deadline:
                break
            method, path, payload, interval = routes[operation]
            t0 = time.perf_counter()
            status, body, cut = await asyncio.wait_for(
                call(reader, writer, method, path, xff, payload, token), timeout=5.0)
            latencies[operation].append(time.perf_counter() - t0)
            bump(statuses, f"{operation} {status}")
            due[operation] = time.perf_counter() + interval
            if operation == "fetch" and status == 200:
                echo = json.loads(body).get("echo")
                if echo is not None:
                    if (echo.get("account") == str(uuid.UUID(int=i + 1))
                            or echo.get("frames") != clip(i)["frames"]
                            or echo.get("actions") != clip(i)["actions"]):
                        bump(statuses, "invalid echo")
                    else:
                        counts["replayed"] += 1
            if cut:
                await close(writer)
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except (ConnectionError, asyncio.IncompleteReadError, OSError, TimeoutError):
        bump(statuses, "connection lost")
    finally:
        if writer is not None and token is not None and not writer.is_closing():
            try:
                await asyncio.wait_for(call(reader, writer, "DELETE", "/v1/session", xff,
                                            token=token), timeout=5.0)
            except (ConnectionError, asyncio.IncompleteReadError, OSError, TimeoutError):
                pass
        await close(writer)


async def attacker(i, port, deadline, mode, warmup, statuses):
    await asyncio.sleep(warmup)
    rng = random.Random(10_000 + i)
    writer = None
    token = None
    xff = f"198.18.{i & 255}.9"
    invalid = clip(i + 100_000)
    invalid["frames"][1][1] = 1536  # Deliberate impossible movement: must be refused.
    try:
        while time.perf_counter() < deadline:
            if writer is None:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
            if mode == "invalid" and token is None:
                status, body, cut = await call(reader, writer, "POST", "/v1/sessions", xff,
                                              {"protocol": 1, "capabilities": ["echoes", "chat"]})
                if status == 201:
                    token = json.loads(body)["token"]
                else:
                    await asyncio.sleep(0.05)
            elif mode == "invalid":
                status, _, cut = await call(reader, writer, "PUT", "/v1/echoes/clip", xff,
                                           invalid, token)
            else:
                xff = f"100.{rng.randrange(64, 128)}.{rng.randrange(256)}.{rng.randrange(1, 255)}"
                status, _, cut = await call(reader, writer, "GET", "/v1/info", xff)
            bump(statuses, str(status))
            if cut:
                await close(writer)
                writer = None
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        bump(statuses, "reset")
    finally:
        await close(writer)


async def run(args):
    port = free_port()
    env = {
        **os.environ, "PYTHONPATH": ROOT,
        "NOCTIS_OPERATOR_NAME": "benchmark", "NOCTIS_OPERATOR_CONTACT": "bench@example.invalid",
        "NOCTIS_PORT": str(port), "NOCTIS_TRUST_PROXY": "1", "NOCTIS_TRUSTED_PROXIES": "127.0.0.1/32",
        "NOCTIS_ECHOES": "1", "NOCTIS_ECHO_CHAT": "1",
        "NOCTIS_ECHO_PUBLISH_INTERVAL": "1", "NOCTIS_ECHO_FETCH_INTERVAL": "1",
    }
    proc = subprocess.Popen([sys.executable, "-B", "-m", "noctis_relay"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if proc.poll() is not None:
                    raise SystemExit("relay exited during startup")
                await asyncio.sleep(0.05)
        else:
            raise SystemExit("relay did not start")
        statuses, attacks, counts = {}, {}, {"replayed": 0}
        latencies = {k: [] for k in ("publish", "fetch", "heartbeat")}
        start = time.perf_counter()
        deadline = start + args.seconds
        tasks = [player(i, port, deadline, statuses, latencies, counts) for i in range(args.players)]
        if args.attack != "none":
            tasks += [attacker(i, port, deadline, args.attack, args.warmup, attacks)
                      for i in range(args.attackers)]
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start
        print(f"{args.players} players, {args.attackers} attackers, attack={args.attack}, {elapsed:.1f}s; "
              "echo pacing 1s, all other abuse limits at defaults")
        print("players:", dict(sorted(statuses.items())), "valid echoes:", counts["replayed"])
        for operation, samples in latencies.items():
            ms = [s * 1000 for s in samples]
            print(f"{operation}: p50 {pct(ms, 50):.2f} ms, p95 {pct(ms, 95):.2f} ms, p99 {pct(ms, 99):.2f} ms")
        print("attacker:", dict(sorted(attacks.items())), f"{sum(attacks.values()) / elapsed:.0f} attempts/s")
        print(f"server RSS: {rss_mb(proc.pid):.1f} MiB")
        good = {"create 201", "publish 200", "fetch 200", "heartbeat 200"}
        bad = {k: v for k, v in statuses.items() if k not in good}
        if args.check and (bad or statuses.get("create 201") != args.players
                           or not all(statuses.get(k) for k in good) or not counts["replayed"]):
            raise SystemExit(f"players were degraded: {bad or 'missing traffic/echoes'}")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--players", type=int, default=50)
    parser.add_argument("--attackers", type=int, default=16)
    parser.add_argument("--attack", choices=("none", "rotate", "invalid"), default="rotate")
    parser.add_argument("--warmup", type=float, default=1.5)
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not 2 <= args.players <= 500 or not 0 <= args.attackers <= 256 or not 2 <= args.seconds <= 60:
        parser.error("use 2–500 players, 0–256 attackers and 2–60 seconds")
    if not 0 <= args.warmup < args.seconds:
        parser.error("warmup must be non-negative and shorter than the run")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
