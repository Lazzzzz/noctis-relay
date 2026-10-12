#!/usr/bin/env python3
"""Publish removable, fictional Noctis action clips through the normal relay API (MIT).

No real Minecraft account, chat, terrain seed or relay administration token is used.
Retain the private --owner-file to remove these examples later with --remove, or to
replace them under the current notice with --renew.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SCENES = ("regard", "marche", "court", "saute", "creuse", "construit", "torches", "passage")
REALM = "astralyth:noctis"
DEFAULT_RELAY = "https://noctis.51-91-242-55.sslip.io"
DEFAULT_PROFILE = "noctis-t1-3fa32a7adc115836"
# Notice revision acknowledged for these fictional owners: zone retention.
PRIVACY_REVISION = 5


def account(scene: str) -> str:
    """UUID.nameUUIDFromBytes, shared with the mod's finite fictional identity list."""
    digest = hashlib.md5(("astralyth:noctis:preview:" + scene).encode(), usedforsecurity=False).digest()
    return str(uuid.UUID(bytes=digest, version=3))


def owner(master: bytes, scene: str) -> str:
    return hmac.new(master, account(scene).encode(), hashlib.sha256).hexdigest()


def master_key(path: Path, *, create: bool) -> bytes:
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise ValueError("owner file must be a regular private file")
    if not path.exists() and create:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(secrets.token_bytes(32))
            output.flush()
            os.fsync(output.fileno())
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("owner file must have permissions 600")
    data = path.read_bytes()
    if len(data) != 32:
        raise ValueError("owner file must contain exactly 32 private bytes")
    return data


def endpoint(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("endpoint must be the relay origin")
    if not parsed.hostname:
        raise ValueError("missing relay host")
    local = parsed.hostname == "localhost"
    try:
        local = local or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("HTTPS required except for local tests")
    return value.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, address: str):
        self.address = endpoint(address)
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method: str, path: str, body=None, token=None, expected=200):
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(self.address + path, data, headers, method=method)
        for attempt in range(12):
            try:
                with self.opener.open(request, timeout=12) as response:
                    raw = response.read(16385)
                    if response.status != expected or len(raw) > 1 << 20:
                        raise ValueError("unexpected or oversized relay response")
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as error:
                retry = error.headers.get("Retry-After", "")
                if error.code in (429, 503) and retry.isdigit() and attempt < 11:
                    wait = max(1, min(60, int(retry)))
                    print(f"Relay quota: waiting {wait}s", flush=True)
                    error.close()
                    time.sleep(wait)
                    continue
                status = error.code
                error.close()
                raise ValueError(f"relay refused request (HTTP {status})") from None
        raise ValueError("relay retry budget exhausted")


def clip(scene: str, x: float, y: float, z: float, profile: str, instance: str) -> dict:
    frames = []
    for tick in range(0, 120, 2):
        u = tick / 118
        dx = dz = dy = 0.0
        flags = 1
        yaw = head = 128  # Face the observer south of this origin.
        if scene in ("regard", "marche", "court"):
            dx = -u * {"regard": 1.2, "marche": 3, "court": 9}[scene]
            flags |= 8 if scene == "regard" else 4 if scene == "court" else 0
        elif scene == "passage":
            dz = u * 4
        elif scene == "saute":
            dy = max(0, math.sin((tick - 20) * math.pi / 38) * 1.1) if 20 <= tick <= 58 else 0
            flags = 0 if dy > .02 else 1
        elif scene in ("creuse", "construit", "torches"):
            flags |= 8
        frames.append([tick, round(dx * 32), round(dy * 32), round(dz * 32), yaw, 0, head, flags])
    actions = []
    if scene in ("regard", "creuse", "construit", "torches"):
        spots = [(-1, -1), (0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0)]
        count = 3 if scene == "regard" else 4 if scene == "torches" else 8
        for i, (bx, bz) in enumerate(spots[:count]):
            actions.append([20 + i * 11, 1 if scene == "creuse" else 2, bx,
                            -1 if scene == "creuse" else 0, bz,
                            "minecraft:soul_torch" if scene == "torches" else "minecraft:stone"])
    return {"realm": REALM, "profile": profile, "instance": instance, "x": x, "y": y, "z": z,
            "account": account(scene), "frames": frames, "actions": actions}


def publish(client: Client, master: bytes, scene: str, body: dict) -> bool:
    session = client.request("POST", "/v1/sessions", {
        "protocol": 1, "capabilities": ["echoes"], "owner": owner(master, scene), "account": account(scene),
        "privacyRevision": PRIVACY_REVISION,
    }, expected=201)
    token = session["token"]
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise ValueError("invalid session response")
    try:
        response = client.request("PUT", "/v1/echoes/clip", body, token)
        return response.get("archived") is True
    finally:
        client.request("DELETE", "/v1/session", token=token, expected=204)


def forget(client: Client, master: bytes, scene: str) -> int:
    response = client.request("POST", "/v1/archive/forget", {
        "protocol": 1, "owner": owner(master, scene), "scope": "all"})
    return response["removed"]


def compatible(info: dict) -> bool:
    """Revision-5 relay keeping archives without a maximum age below 92 days."""
    archive = info.get("archive")
    if info.get("protocol") != 1 or info.get("privacyRevision") != PRIVACY_REVISION:
        return False
    if "echoes" not in info.get("capabilities", []) or not isinstance(archive, dict) or "maxAgeDays" not in archive:
        return False
    limit = archive.get("maxAgeDays")
    return limit is None or (isinstance(limit, int) and limit >= 92)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_RELAY)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--x", type=float)
    parser.add_argument("--y", type=float)
    parser.add_argument("--z", type=float)
    parser.add_argument("--owner-file", type=Path, required=True)
    parser.add_argument("--scene", choices=SCENES, action="append")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--remove", action="store_true")
    action.add_argument("--renew", action="store_true",
                        help="remove each scene's earlier clips, then publish it again under the current notice")
    args = parser.parse_args()
    try:
        client = Client(args.endpoint)
        key = master_key(args.owner_file, create=not args.remove)
        scenes = args.scene or SCENES
        if args.remove:
            for scene in scenes:
                print(f"Removed {scene}: {forget(client, key, scene)} archived clips", flush=True)
            return 0
        if any(v is None or not math.isfinite(v) for v in (args.x, args.y, args.z)):
            raise ValueError("provide finite --x --y --z at the desired Noctis surface")
        if abs(args.x) > 30_000_000 or abs(args.z) > 30_000_000 or not -2048 <= args.y <= 4096:
            raise ValueError("coordinates out of bounds")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", args.profile):
            raise ValueError("invalid terrain profile")
        if not compatible(client.request("GET", "/v1/info")):
            raise ValueError("incompatible relay: revision 5 archive without a maximum age under 92 days required")
        instance = "preview_" + secrets.token_hex(16)
        stored = 0
        for scene in scenes:
            if args.renew:
                print(f"{scene}: removed {forget(client, key, scene)} earlier clips", flush=True)
            admitted = publish(client, key, scene, clip(scene, args.x, args.y, args.z, args.profile, instance))
            stored += admitted
            print(f"{scene}: {'archived' if admitted else 'not archived (sampling or capacity)'}", flush=True)
        print(f"Stored {stored}/{len(scenes)} fictional scenes; source sessions revoked", flush=True)
        return 0 if stored == len(scenes) else 2
    except (ValueError, OSError, urllib.error.URLError) as error:
        # Do not print request headers, response contents, keys or bearer tokens.
        print(f"Seed failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
