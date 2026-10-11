"""End-to-end smoke test of the echoes capability against a running relay.

    NOCTIS_ECHOES=1 NOCTIS_ECHO_CHAT=1 NOCTIS_CREATE_BURST=8 python3 -m noctis_relay &
    python3 bench/smoke_echoes.py --url http://127.0.0.1:8787

Opens five sessions from this host: one publishes a clip with a chat line,
another fetches it (with chat), a third fetches it without the chat
capability, then the publisher withdraws and a last one sees nothing. Also
checks that a teleporting clip and a clip from the observer's own game
instance are never served. Standard library only; exits non-zero on failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

REALM = "astralyth:noctis"
PROFILE = "noctis-t1-smoke0123456789"
ACCOUNT = "0f1e2d3c-4b5a-4968-8776-655443322110"


def decode(raw):
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def call(base, method, path, body=None, token=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, decode(raw)
    except urllib.error.HTTPError as err:
        raw = err.read()
        return err.code, decode(raw)


def expect(cond, message):
    if not cond:
        print("FAIL", message)
        sys.exit(1)
    print("ok  ", message)


def session(base, caps):
    status, body = call(base, "POST", "/v1/sessions", {"protocol": 1, "capabilities": caps})
    expect(status == 201 and body.get("capabilities") == caps, f"session {caps} -> {status}")
    return body["token"]


def place(instance, x=100.0):
    return {"realm": REALM, "profile": PROFILE, "instance": instance, "x": x, "y": 70.0, "z": -40.0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8787")
    ap.add_argument("--wait", type=float, default=15.0, help="seconds to wait for /healthz")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    deadline = time.monotonic() + args.wait
    while True:
        try:
            if call(base, "GET", "/healthz")[0] == 200:
                break
        except OSError:
            pass
        if time.monotonic() > deadline:
            expect(False, "relay reachable")
        time.sleep(0.3)

    status, info = call(base, "GET", "/v1/info")
    expect(status == 200 and info["privacyRevision"] == 3, "info privacy revision 3")
    expect("echoes" in info["capabilities"] and "chat" in info["capabilities"], "echoes and chat offered")
    expect(info["echoes"]["maxBodyBytes"] >= 1024, "echo settings published")

    publisher = session(base, ["echoes", "chat"])
    teleport = {
        **place("smoke-instance-publisher"), "account": ACCOUNT,
        "frames": [[0, 0, 0, 0, 0, 0, 0, 1], [1, 1536, 0, 0, 0, 0, 0, 1]],
    }
    status, body = call(base, "PUT", "/v1/echoes/clip", teleport, publisher)
    expect(status == 400 and body["error"] == "implausible_movement", "teleporting clip refused")

    clip = {
        **place("smoke-instance-publisher"), "account": ACCOUNT,
        "frames": [[2 * i, 6 * i, 0, 0, 64, 0, 64, 1] for i in range(30)],
        "actions": [[10, 1, 2, 0, 0, "minecraft:stone"]],
        "chat": [[20, "hello from another world"]],
    }
    status, body = call(base, "PUT", "/v1/echoes/clip", clip, publisher)
    expect(status == 200 and body["chat"] == 1, "clip published with one chat line")

    same_game = session(base, ["echoes"])
    status, body = call(base, "POST", "/v1/echoes/next", place("smoke-instance-publisher", 110.0), same_game)
    expect(status == 200 and body["echo"] is None, "same game instance excluded")

    viewer = session(base, ["echoes", "chat"])
    status, body = call(base, "POST", "/v1/echoes/next", place("smoke-instance-viewer", 110.0), viewer)
    echo = body and body.get("echo")
    expect(status == 200 and echo and echo["account"] == ACCOUNT, "nearby echo served")
    expect(len(echo["frames"]) == 30 and echo["actions"][0][5] == "minecraft:stone", "frames and actions intact")
    expect(echo.get("chat") == [[20, "hello from another world"]], "chat served to a chat session")
    expect(set(echo) <= {"ref", "ageSeconds", "x", "y", "z", "account", "frames", "actions", "chat"},
           "no extra fields")

    quiet = session(base, ["echoes"])
    status, body = call(base, "POST", "/v1/echoes/next", place("smoke-instance-quiet", 110.0), quiet)
    echo = body and body.get("echo")
    expect(status == 200 and echo and "chat" not in echo, "chat withheld from a session without chat")

    status, body = call(base, "DELETE", "/v1/session", None, publisher)
    expect(status == 204, "publisher withdraws")
    late = session(base, ["echoes"])
    status, body = call(base, "POST", "/v1/echoes/next", place("smoke-instance-late", 110.0), late)
    expect(status == 200 and body["echo"] is None, "withdrawn clip no longer served")

    for token in (same_game, viewer, quiet, late):
        call(base, "DELETE", "/v1/session", None, token)
    print("echoes smoke: PASS")


if __name__ == "__main__":
    main()
