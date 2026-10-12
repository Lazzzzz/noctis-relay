"""Strict validation of the optional ``gear`` field of an echo clip.

The gear is the visible equipment of the replayed body: each worn or held item
as a binary NBT ``ItemStack`` (id, count, components), plus a timeline of slot
changes and of a few body poses. The relay never interprets item data; it only
checks a bounded structure and stores the canonical form, so no client byte
reaches another player outside these budgets.

Wiring (server.py): accept ``gear`` as an optional clip key, call
``parse_gear(obj["gear"], frames[-1][0])`` and turn ``GearError`` into
``HttpError(400, "invalid_gear")``; store ``canonical_json(gear)`` with the clip
and announce ``INFO`` under ``info["echoes"]["gear"]``. Clients send ``gear``
only to a relay announcing it. See build/noctis-equipment-protocol.md.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import struct

VERSION = 1
# Generous enough that a real minute of play is never shortened: every swap
# of every slot, a pose change per tick, rich modded items.
MAX_ITEMS = 64
MAX_ITEM_BYTES = 8192
MAX_ITEMS_BYTES = 98_304
MAX_GEAR_JSON = 262_144
MAX_SLOT_ENTRIES = 1024
MAX_SLOT_NAMES = 16
MAX_TICK = 1200
MAX_POSE_ENTRIES = MAX_TICK + 1
POSE_BITS = 0b111111  # fall flying, using, using off hand, swing off hand, left handed, spin attack
POSE_USING, POSE_USE_OFFHAND = 2, 4
MAX_POWER = 100
NBT_MAX_DEPTH = 12
NBT_MAX_TAGS = 512
NBT_MAX_LENGTH = 256
NBT_MAX_STRING = 1024
MAX_COMPONENTS = 64

ITEM_ID_RE = re.compile(r"[a-z0-9_.-]{1,32}:[a-z0-9_./-]{1,64}")
COMPONENT_RE = re.compile(r"!?[a-z0-9_.-]{1,32}:[a-z0-9_./-]{1,64}")
SLOT_RE = re.compile(r"head|chest|legs|feet|mainhand|offhand|[a-z0-9_.-]{1,24}:[a-z0-9_./-]{1,32}")
BASE64_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")

# Components holding private or unrelated state: stored contents, writings,
# places, captured entities or blocks. Names, lore, custom data and modded
# components stay, since models and resource packs can select on them; nothing
# here is shown as text by a ghost. Clients strip these; the relay refuses a
# clip that still carries one, as set or as removed ("!").
DENIED_COMPONENTS = frozenset(
    "minecraft:" + name
    for name in (
        "container", "bundle_contents", "writable_book_content", "written_book_content",
        "lodestone_tracker", "map_id", "map_decorations", "entity_data", "bucket_entity_data",
        "block_entity_data", "bees", "container_loot", "recipes", "debug_stick_state",
    )
)

INFO = {
    "version": VERSION,
    "maxItems": MAX_ITEMS,
    "maxItemBytes": MAX_ITEM_BYTES,
    "maxItemsBytes": MAX_ITEMS_BYTES,
    "maxJsonBytes": MAX_GEAR_JSON,
    "maxSlotEntries": MAX_SLOT_ENTRIES,
    "maxSlotNames": MAX_SLOT_NAMES,
    "maxPoseEntries": MAX_POSE_ENTRIES,
    "maxDepth": NBT_MAX_DEPTH,
    "maxTags": NBT_MAX_TAGS,
}

_END, _BYTE, _SHORT, _INT, _LONG, _FLOAT, _DOUBLE = 0, 1, 2, 3, 4, 5, 6
_BYTE_ARRAY, _STRING, _LIST, _COMPOUND, _INT_ARRAY, _LONG_ARRAY = 7, 8, 9, 10, 11, 12
_FIXED = {_BYTE: 1, _SHORT: 2, _INT: 4, _LONG: 8}


class GearError(ValueError):
    """The gear field breaks the schema; the whole clip is refused."""


class _Reader:
    """Bounded big-endian reader over one item's NBT bytes (NbtIo format)."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.tags = 0

    def take(self, n: int) -> bytes:
        end = self.pos + n
        if end > len(self.data):
            raise GearError("truncated")
        chunk = self.data[self.pos:end]
        self.pos = end
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def s32(self) -> int:
        return struct.unpack(">i", self.take(4))[0]

    def string(self) -> str:
        n = struct.unpack(">H", self.take(2))[0]
        if n > NBT_MAX_STRING:
            raise GearError("string")
        return _modified_utf8(self.take(n))

    def count(self) -> None:
        self.tags += 1
        if self.tags > NBT_MAX_TAGS:
            raise GearError("tags")

    def payload(self, kind: int, depth: int):
        self.count()
        if kind in _FIXED:
            return int.from_bytes(self.take(_FIXED[kind]), "big", signed=True)
        if kind in (_FLOAT, _DOUBLE):
            value = struct.unpack(">f" if kind == _FLOAT else ">d", self.take(4 if kind == _FLOAT else 8))[0]
            if not math.isfinite(value):
                raise GearError("number")
            return value
        if kind == _STRING:
            return self.string()
        if kind in (_BYTE_ARRAY, _INT_ARRAY, _LONG_ARRAY):
            n = self.s32()
            if not 0 <= n <= NBT_MAX_LENGTH:
                raise GearError("length")
            self.take(n * {_BYTE_ARRAY: 1, _INT_ARRAY: 4, _LONG_ARRAY: 8}[kind])
            return None
        if depth >= NBT_MAX_DEPTH:
            raise GearError("depth")
        if kind == _LIST:
            element = self.u8()
            n = self.s32()
            if not 0 <= n <= NBT_MAX_LENGTH or element > _LONG_ARRAY or (element == _END) != (n == 0):
                raise GearError("list")
            for _ in range(n):
                self.payload(element, depth + 1)
            return None
        if kind == _COMPOUND:
            out = {}
            while True:
                child = self.u8()
                if child == _END:
                    return out
                if child > _LONG_ARRAY:
                    raise GearError("type")
                key = self.string()
                if key in out:
                    raise GearError("duplicate")
                out[key] = (child, self.payload(child, depth + 1))
        raise GearError("type")


def _modified_utf8(raw: bytes) -> str:
    """Java DataInput.readUTF bytes, refusing NUL bytes, overlong and 4-byte forms."""
    out = []
    i, n = 0, len(raw)
    while i < n:
        b = raw[i]
        if 0 < b < 0x80:
            out.append(b)
            i += 1
        elif 0xC0 <= b < 0xE0 and i + 1 < n and raw[i + 1] & 0xC0 == 0x80:
            c = (b & 0x1F) << 6 | raw[i + 1] & 0x3F
            if c < 0x80 and c != 0:
                raise GearError("string")
            out.append(c)
            i += 2
        elif 0xE0 <= b < 0xF0 and i + 2 < n and raw[i + 1] & 0xC0 == 0x80 and raw[i + 2] & 0xC0 == 0x80:
            c = (b & 0x0F) << 12 | (raw[i + 1] & 0x3F) << 6 | raw[i + 2] & 0x3F
            if c < 0x800:
                raise GearError("string")
            out.append(c)
            i += 3
        else:
            raise GearError("string")
    return "".join(map(chr, out))


def parse_item(data: bytes) -> dict:
    """Checks one ``NbtIo.write`` of an ``ItemStack.CODEC`` value; returns its top level."""
    if not 1 <= len(data) <= MAX_ITEM_BYTES:
        raise GearError("item_size")
    reader = _Reader(data)
    if reader.u8() != _COMPOUND or reader.take(2) != b"\x00\x00":
        raise GearError("root")
    root = reader.payload(_COMPOUND, 0)
    if reader.pos != len(data):
        raise GearError("trailing")
    if not {"id", "count"} <= set(root) <= {"id", "count", "components"}:
        raise GearError("item_keys")
    kind, ident = root["id"]
    if kind != _STRING or not ITEM_ID_RE.fullmatch(ident) or ident == "minecraft:air":
        raise GearError("item_id")
    kind, count = root["count"]
    if kind != _INT or not 1 <= count <= 99:
        raise GearError("item_count")
    if "components" in root:
        kind, components = root["components"]
        if kind != _COMPOUND or not 1 <= len(components) <= MAX_COMPONENTS:
            raise GearError("components")
        for key, (kind, value) in components.items():
            if not COMPONENT_RE.fullmatch(key) or key.lstrip("!") in DENIED_COMPONENTS:
                raise GearError("component")
            # A removed default carries Codec.EMPTY: an empty compound.
            if key.startswith("!") and (kind != _COMPOUND or value):
                raise GearError("component")
    return root


def _int_in(value, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise GearError("range")
    return value


def _items(raw) -> list[str]:
    if not isinstance(raw, list) or len(raw) > MAX_ITEMS:
        raise GearError("items")
    total = 0
    seen = set()
    for text in raw:
        if not isinstance(text, str) or len(text) > 4 * MAX_ITEM_BYTES // 3 + 4 or not BASE64_RE.fullmatch(text):
            raise GearError("base64")
        try:
            data = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            raise GearError("base64") from None
        if base64.b64encode(data).decode("ascii") != text:
            raise GearError("base64")
        if data in seen:
            raise GearError("duplicate_item")
        seen.add(data)
        parse_item(data)
        total += len(data)
        if total > MAX_ITEMS_BYTES:
            raise GearError("items_size")
    return list(raw)


def _slots(raw, end: int, items: int) -> list:
    if not isinstance(raw, list) or len(raw) > MAX_SLOT_ENTRIES:
        raise GearError("slots")
    state: dict[str, int] = {}
    seen: set[tuple[int, str]] = set()
    used = set()
    out = []
    last = 0
    for entry in raw:
        if not isinstance(entry, list) or len(entry) != 3:
            raise GearError("slots")
        t = _int_in(entry[0], last, end)
        slot = entry[1]
        if not isinstance(slot, str) or not SLOT_RE.fullmatch(slot):
            raise GearError("slot")
        item = _int_in(entry[2], -1, items - 1)
        # Each entry changes its slot (-1 empties it; every slot starts empty).
        if (t, slot) in seen or state.get(slot, -1) == item:
            raise GearError("slot_change")
        seen.add((t, slot))
        state[slot] = item
        if len(state) > MAX_SLOT_NAMES:
            raise GearError("slot_names")
        used.add(item)
        last = t
        out.append([t, slot, item])
    if any(i not in used for i in range(items)):
        raise GearError("unused_item")
    return out


def _pose(raw, end: int) -> list:
    if not isinstance(raw, list) or len(raw) > MAX_POSE_ENTRIES:
        raise GearError("pose")
    out = []
    for entry in raw:
        if not isinstance(entry, list) or len(entry) != 3:
            raise GearError("pose")
        t = _int_in(entry[0], 0 if not out else out[-1][0] + 1, end)
        bits = _int_in(entry[1], 0, POSE_BITS)
        power = _int_in(entry[2], 0, MAX_POWER)
        if bits & POSE_USE_OFFHAND and not bits & POSE_USING:
            raise GearError("pose_bits")
        # The pose starts neutral at tick 0; each entry is a change.
        previous = out[-1][1:] if out else [0, 0]
        if [bits, power] == previous:
            raise GearError("pose_change")
        out.append([t, bits, power])
    return out


def parse_gear(raw, end_tick: int) -> dict:
    """Validated canonical gear for a clip whose last frame is at ``end_tick``."""
    if not isinstance(raw, dict) or set(raw) != {"items", "slots", "pose"}:
        raise GearError("fields")
    end = _int_in(end_tick, 0, MAX_TICK)
    items = _items(raw["items"])
    gear = {"items": items, "slots": _slots(raw["slots"], end, len(items)), "pose": _pose(raw["pose"], end)}
    if not gear["slots"] and not gear["pose"]:
        raise GearError("empty")
    if len(canonical_json(gear)) > MAX_GEAR_JSON:
        raise GearError("gear_size")
    return gear


def canonical_json(gear: dict) -> str:
    """The stored and served form: compact, ASCII only (base64 and slot names)."""
    return json.dumps(gear, separators=(",", ":"), ensure_ascii=True)
