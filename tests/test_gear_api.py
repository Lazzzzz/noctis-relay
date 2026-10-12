"""Optional ``gear`` of echo clips through the API: upload, live and archived replay, limits."""

import json
import struct
import unittest

from noctis_relay import equipment

from .test_echoes import ACCOUNT, clip, maximal_clip, place, walk
from .test_equipment import CHEST, JETPACK, b64, gear, item, maximal_gear
from .test_zone_retention import RelayFixture

LONG = walk(n=30)  # ticks 0..58: room for the sample gear timeline


class GearApiTest(RelayFixture, unittest.TestCase):
    def test_info_announces_gear_and_upload_size(self):
        echoes = self.r.request("GET", "/v1/info")[1]["echoes"]
        self.assertEqual(echoes["gear"], equipment.INFO)
        self.assertEqual(echoes["maxBodyBytes"], 524288)

    def test_gear_replayed_live_and_from_the_archive(self):
        a = self.owner_session(privacyRevision=5)
        sent = gear()
        status, body, _ = self.publish(a, clip(frames=LONG, gear=sent))
        self.assertEqual((status, body["archived"]), (200, True))
        o = self.r.create(capabilities=["echoes"])
        echo = self.fetch(o)[1]["echo"]
        self.assertEqual(echo["gear"], equipment.parse_gear(sent, LONG[-1][0]))
        self.r.request("DELETE", "/v1/session", token=a)
        self.stop()
        self.later(200 * 86400)
        self.start()
        echo = self.fetch(self.r.create(capabilities=["echoes"]))[1]["echo"]
        self.assertEqual(echo["gear"], sent)
        self.assertEqual(echo["frames"], LONG)
        self.assertEqual(self.r.request("POST", "/v1/archive/forget",
                                        {"protocol": 1, "owner": "a" * 64, "scope": "all"})[1], {"removed": 1})

    def test_clip_without_gear_has_none(self):
        a = self.owner_session()
        self.assertEqual(self.publish(a, clip())[0], 200)
        self.assertNotIn("gear", self.fetch(self.r.create(capabilities=["echoes"]))[1]["echo"])

    def test_invalid_gear_refuses_the_whole_clip(self):
        a = self.owner_session()
        container = item("minecraft:shulker_box", components=[("minecraft:container", (9, b"\x00" + b"\x00" * 4))])
        for bad in (
            gear(),  # timeline past the last frame (tick 10)
            {**gear(), "extra": 1},
            gear(items=[b64(JETPACK), b64(container)]),
            gear(items=[b64(CHEST)], slots=[[0, "chest", 0]], pose="x"),
            [],
        ):
            status, body, _ = self.publish(a, clip(gear=bad))
            self.assertEqual((status, body["error"]), (400, "invalid_gear"), bad)
        self.assertIsNone(self.fetch(self.r.create(capabilities=["echoes"]))[1]["echo"])
        self.assertEqual(self.archive.rows, 0)

    def test_maximal_clip_with_maximal_gear_fits_the_upload(self):
        a = self.owner_session(privacyRevision=5)
        big = maximal_gear()
        body = maximal_clip()
        without = len(json.dumps(body, separators=(",", ":")).encode())
        # Any gear the relay accepts fits beside the largest clip (compact JSON as sent by the mod).
        self.assertLessEqual(without + len(',"gear":') + equipment.MAX_GEAR_JSON, 524288)
        body["gear"] = big
        status, resp, _ = self.publish(a, body)
        self.assertEqual(status, 200, resp)
        o = self.r.create(capabilities=["echoes", "chat"])
        status, payload, _ = self.r.request("POST", "/v1/echoes/next",
                                            {**place(), "realm": body["realm"], "profile": body["profile"],
                                             "x": body["x"], "y": body["y"], "z": body["z"]}, token=o)
        self.assertEqual(status, 200)
        self.assertEqual(payload["echo"]["gear"], big)
        self.assertEqual(len(payload["echo"]["frames"]), 1201)
        self.assertEqual(len(payload["echo"]["actions"]), 1024)
        self.assertLess(len(json.dumps(payload).encode()), 1 << 20)

class GearDisabledTest(RelayFixture, unittest.TestCase):
    gear = False

    def test_operator_can_turn_gear_off(self):
        self.assertNotIn("gear", self.r.request("GET", "/v1/info")[1]["echoes"])
        a = self.owner_session()
        status, body, _ = self.publish(a, clip(frames=LONG, gear=gear()))
        self.assertEqual((status, body["error"]), (400, "invalid_fields"))
        self.assertEqual(self.publish(a, clip(account=ACCOUNT))[0], 200)


if __name__ == "__main__":
    unittest.main()
