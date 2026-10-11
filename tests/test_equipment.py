import base64
import json
import struct
import unittest

from noctis_relay import equipment
from noctis_relay.equipment import GearError, canonical_json, parse_gear, parse_item


def utf(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def string(text: str) -> tuple:
    return (8, utf(text))


def integer(value: int) -> tuple:
    return (3, struct.pack(">i", value))


def compound(**entries) -> tuple:
    body = b"".join(bytes([kind]) + utf(key.replace("__", ":")) + payload for key, (kind, payload) in entries.items())
    return (10, body + b"\x00")


def compound_of(pairs) -> tuple:
    body = b"".join(bytes([kind]) + utf(key) + payload for key, (kind, payload) in pairs)
    return (10, body + b"\x00")


def lst(kind: int, payloads: list) -> tuple:
    return (9, bytes([kind]) + struct.pack(">i", len(payloads)) + b"".join(payloads))


def root(value: tuple) -> bytes:
    """NbtIo.write: type byte, empty name, payload."""
    return bytes([value[0]]) + utf("") + value[1]


def item(ident="minecraft:diamond_chestplate", count=1, components=None) -> bytes:
    pairs = [("id", string(ident)), ("count", integer(count))]
    if components is not None:
        pairs.append(("components", compound_of(components)))
    return root(compound_of(pairs))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


JETPACK = item(
    "astralyth:jetpack",
    components=[
        ("minecraft:custom_data", compound_of([("Charge", integer(812)), ("Tier", integer(2))])),
        ("minecraft:dyed_color", integer(0x3366FF)),
        ("!minecraft:enchantable", compound_of([])),
    ],
)
CHEST = item("minecraft:diamond_chestplate")


def gear(**overrides) -> dict:
    base = {
        "items": [b64(JETPACK), b64(CHEST)],
        "slots": [[0, "astralyth:back", 0], [0, "chest", 1], [40, "chest", -1]],
        "pose": [[0, 1, 80], [20, 0, 0]],
    }
    base.update(overrides)
    return base


class GearTest(unittest.TestCase):
    def assertRefused(self, raw, end=60):
        with self.assertRaises(GearError):
            parse_gear(raw, end)

    def test_valid_gear_is_canonical_and_compact(self):
        parsed = parse_gear(gear(), 60)
        self.assertEqual(parsed, gear())
        text = canonical_json(parsed)
        self.assertEqual(json.loads(text), parsed)
        self.assertNotIn(" ", text)

    def test_item_structure_is_readable(self):
        top = parse_item(JETPACK)
        self.assertEqual(top["id"], (8, "astralyth:jetpack"))
        self.assertIn("minecraft:custom_data", top["components"][1])

    def test_unknown_or_missing_fields(self):
        self.assertRefused({"items": [], "slots": [], "pose": [[0, 1, 0]], "extra": 1})
        self.assertRefused({"items": [], "slots": []})
        self.assertRefused([])

    def test_empty_gear_is_omitted_not_sent(self):
        self.assertRefused({"items": [], "slots": [], "pose": []})
        self.assertEqual(parse_gear({"items": [], "slots": [], "pose": [[0, 32, 0]]}, 0)["pose"], [[0, 32, 0]])

    def test_booleans_and_floats_are_not_ticks(self):
        self.assertRefused(gear(pose=[[True, 1, 80]]))
        self.assertRefused(gear(pose=[[0.0, 1, 80]]))
        self.assertRefused(gear(slots=[[0, "astralyth:back", False], [0, "chest", 1]]))

    def test_base64_must_be_canonical(self):
        self.assertRefused(gear(items=[b64(JETPACK) + "\n", b64(CHEST)]))
        padded = next(s for s in (item(f"mod:{'e' * n}") for n in range(1, 4)) if len(s) % 3 == 1)
        text = b64(padded)
        self.assertTrue(text.endswith("=="))
        slots = [[0, "astralyth:back", 0], [0, "chest", 1]]
        self.assertRefused(gear(items=[b64(JETPACK), text.rstrip("=")], slots=slots))
        # Same bytes, but the unused low bits of the last digit are set.
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        bad = text[:-3] + alphabet[alphabet.index(text[-3]) | 1] + "=="
        self.assertEqual(base64.b64decode(bad), padded)
        self.assertRefused(gear(items=[b64(JETPACK), bad], slots=slots))
        parse_gear(gear(items=[b64(JETPACK), text], slots=slots), 60)

    def test_items_must_be_distinct_and_used(self):
        self.assertRefused(gear(items=[b64(CHEST), b64(CHEST)]))
        self.assertRefused(gear(slots=[[0, "astralyth:back", 0]]))

    def test_slot_timeline(self):
        self.assertRefused(gear(slots=[[0, "chest", 1], [0, "astralyth:back", 0], [0, "chest", 1]]))
        self.assertRefused(gear(slots=[[10, "astralyth:back", 0], [0, "chest", 1]]))  # not ordered
        self.assertRefused(gear(slots=[[0, "astralyth:back", 0], [0, "chest", 1], [61, "chest", -1]]))
        self.assertRefused(gear(slots=[[0, "astralyth:back", 0], [0, "chest", 1], [10, "chest", 1]]))  # no change
        self.assertRefused(gear(slots=[[0, "astralyth:back", 0], [0, "chest", 1], [0, "legs", -1]]))  # starts empty
        self.assertRefused(gear(slots=[[0, "astralyth:back", 0], [0, "Chest", 1]]))
        self.assertRefused(gear(slots=[[0, "astralyth:back", 0], [0, "chest", 2]]))
        names = [[0, f"mod:s{i}", 0 if i == 0 else -1] for i in range(1)]
        names += [[1 + i, f"mod:s{i % 17}", i % 2] for i in range(17)]
        self.assertRefused(gear(slots=names))

    def test_pose_timeline(self):
        self.assertRefused(gear(pose=[[0, 1, 80], [0, 0, 0]]))
        self.assertRefused(gear(pose=[[0, 0, 0]]))  # neutral is the start
        self.assertRefused(gear(pose=[[0, 1, 80], [20, 1, 80]]))
        self.assertRefused(gear(pose=[[0, 4, 0]]))  # off hand use without use
        self.assertRefused(gear(pose=[[0, 64, 0]]))
        self.assertRefused(gear(pose=[[0, 1, 101]]))
        self.assertRefused(gear(pose=[[i, i % 2 + 1, 0] for i in range(65)]), end=120)

    def test_item_root_and_keys(self):
        named = bytes([10]) + utf("x") + compound(id=string("minecraft:elytra"), count=integer(1))[1]
        for bad in (
            named,
            root(string("minecraft:elytra")),
            root(compound(id=string("minecraft:elytra"))),
            root(compound(id=string("minecraft:elytra"), count=integer(1), slot=integer(3))),
            item("minecraft:air"),
            item("Minecraft:Elytra"),
            item(count=0),
            item(count=100),
            item(components=[]),
            item() + b"\x00",
            item()[:-1],
        ):
            with self.subTest(bad=bad[:24]), self.assertRaises(GearError):
                parse_item(bad)

    def test_private_components_are_refused(self):
        for name in sorted(equipment.DENIED_COMPONENTS):
            with self.subTest(name=name), self.assertRaises(GearError):
                parse_item(item(components=[(name, compound_of([]))]))
            with self.assertRaises(GearError):
                parse_item(item(components=[("!" + name, compound_of([]))]))

    def test_appearance_components_are_kept(self):
        """Names, lore, custom and modded data can drive models: they stay within budgets."""
        text = (8, utf('{"text":"Aile de givre"}'))
        top = parse_item(item(components=[
            ("minecraft:custom_name", text),
            ("minecraft:item_name", text),
            ("minecraft:lore", lst(8, [utf('"ligne"')])),
            ("minecraft:custom_data", compound_of([("Glow", integer(3))])),
            ("othermod:paint", compound_of([("Layer", integer(1))])),
            ("!minecraft:enchantment_glint_override", compound_of([])),
        ]))
        self.assertIn("minecraft:custom_name", top["components"][1])

    def test_removed_component_carries_nothing(self):
        with self.assertRaises(GearError):
            parse_item(item(components=[("!minecraft:dyed_color", integer(1))]))
        with self.assertRaises(GearError):
            parse_item(item(components=[("!minecraft:dyed_color", compound_of([("a", integer(1))]))]))

    def test_nbt_budgets(self):
        deep = compound_of([])
        for _ in range(equipment.NBT_MAX_DEPTH):
            deep = compound_of([("d", deep)])
        with self.assertRaises(GearError):
            parse_item(item(components=[("mod:deep", deep)]))
        wide = lst(3, [struct.pack(">i", i) for i in range(equipment.NBT_MAX_LENGTH + 1)])
        with self.assertRaises(GearError):
            parse_item(item(components=[("mod:wide", wide)]))
        many = [(f"k{i}", compound_of([(f"v{j}", integer(j)) for j in range(40)])) for i in range(14)]
        with self.assertRaises(GearError):
            parse_item(item(components=[("mod:many", compound_of(many))]))
        array = (11, struct.pack(">i", -1))
        with self.assertRaises(GearError):
            parse_item(item(components=[("mod:array", array)]))
        with self.assertRaises(GearError):
            parse_item(item(components=[("mod:end", (9, b"\x00" + struct.pack(">i", 2)))]))
        with self.assertRaises(GearError):
            parse_item(item(components=[("mod:empty", (9, b"\x03" + struct.pack(">i", 0)))]))

    def test_strings_and_numbers(self):
        for payload in (
            (8, b"\x00\x01\x00"),  # raw NUL
            (8, b"\x00\x04\xf0\x9f\x98\x80"),  # 4-byte UTF-8
            (8, b"\x00\x02\xc1\x81"),  # overlong
            (8, b"\x00\x01\xc3"),  # truncated sequence
            (5, struct.pack(">f", float("nan"))),
            (6, struct.pack(">d", float("inf"))),
            (8, struct.pack(">H", 1025) + b"a" * 1025),
        ):
            with self.subTest(payload=payload[1][:8]), self.assertRaises(GearError):
                parse_item(item(components=[("mod:value", payload)]))
        # Java writes NUL as C0 80: accepted.
        parse_item(item(components=[("mod:value", (8, b"\x00\x02\xc0\x80"))]))

    def test_duplicate_compound_key(self):
        twice = (10, b"\x03" + utf("a") + struct.pack(">i", 1) + b"\x03" + utf("a") + struct.pack(">i", 2) + b"\x00")
        with self.assertRaises(GearError):
            parse_item(item(components=[("mod:twice", twice)]))

    def test_size_budgets(self):
        big = item(components=[("mod:pad", (7, struct.pack(">i", 250) + b"x" * 250)) for _ in range(1)])
        parse_item(big)
        huge = item(components=[(f"mod:pad{i}", (7, struct.pack(">i", 250) + b"x" * 250)) for i in range(9)])
        with self.assertRaises(GearError):
            parse_item(huge)
        stacks = [item(f"mod:i{i}", components=[(f"mod:pad{j}", (7, struct.pack(">i", 240) + b"x" * 240)) for j in range(6)]) for i in range(5)]
        self.assertTrue(all(len(s) <= equipment.MAX_ITEM_BYTES for s in stacks))
        self.assertGreater(sum(map(len, stacks)), equipment.MAX_ITEMS_BYTES)
        slots = [[0, f"mod:s{i}", i] for i in range(5)]
        self.assertRefused({"items": [b64(s) for s in stacks], "slots": slots, "pose": []})

    def test_worst_case_fits_upload(self):
        """Largest accepted gear stays under the canonical JSON cap."""
        stacks = [item(f"mod:i{i}", components=[(f"mod:p{j}", (7, struct.pack(">i", 240) + b"x" * 240)) for j in range(1)]) for i in range(12)]
        slots = [[i // 4 * 10, f"slot{i % 12:02d}-name-padding-x:{'p' * 30}", i % 12] for i in range(12)]
        slots += [[100 + i, f"slot{i % 12:02d}-name-padding-x:{'p' * 30}", -1] for i in range(12)]
        pose = [[i, 1 + i % 2, i % 101] for i in range(64)]
        parsed = parse_gear({"items": [b64(s) for s in stacks], "slots": slots, "pose": pose}, 120)
        self.assertLessEqual(len(canonical_json(parsed)), equipment.MAX_GEAR_JSON)


if __name__ == "__main__":
    unittest.main()
