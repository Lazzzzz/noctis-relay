"""Fictional seeds use the public API and survive source logout, then are removable."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

from noctis_relay.archive import Archive
from .support import RunningRelay, make_config

spec = importlib.util.spec_from_file_location("seed_previews", Path(__file__).parents[1] / "tools/seed_previews.py")
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)


class SeedPreviewsTest(unittest.TestCase):
    def test_all_scenes_archive_after_source_logout_and_private_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            key = seed.master_key(Path(directory) / "owners", create=True)
            self.assertEqual(seed.master_key(Path(directory) / "owners", create=False), key)
            archive = Archive(str(Path(directory) / "archive.db"), min_age=0)
            config = make_config(echoes_enabled=True, archive_path=archive.path,
                                 archive_rate=0, global_echo_rate=0, echo_fetch_interval=0)
            relay = RunningRelay(config, archive=archive)
            try:
                class Adapter:
                    def request(self, method, path, body=None, token=None, expected=200):
                        status, response, _ = relay.request(method, path, body, token=token)
                        if status != expected:
                            raise AssertionError((status, response))
                        return response
                client = Adapter()
                for scene in seed.SCENES:
                    self.assertTrue(seed.publish(client, key, scene,
                        seed.clip(scene, .5, 81, .5, seed.DEFAULT_PROFILE, "preview_fictional_instance")), scene)
                    self.assertEqual(relay.relay.store.session_count(), 0)
                observer = relay.create(capabilities=["echoes"])
                place = {"realm": seed.REALM, "profile": seed.DEFAULT_PROFILE,
                         "instance": "observer_another_game", "x": .5, "y": 81, "z": .5}
                seen = set()
                for _ in seed.SCENES:
                    status, response, _ = relay.request("POST", "/v1/echoes/next", place, token=observer)
                    self.assertEqual(status, 200)
                    self.assertIsNotNone(response["echo"])
                    seen.add(response["echo"]["account"])
                    self.assertNotIn("chat", response["echo"])
                self.assertEqual(seen, {seed.account(scene) for scene in seed.SCENES})
                for scene in seed.SCENES:
                    status, response, _ = relay.request("POST", "/v1/archive/forget", {
                        "protocol": 1, "owner": seed.owner(key, scene), "scope": "all"})
                    self.assertEqual((status, response["removed"]), (200, 1))
                self.assertEqual(archive.rows, 0)
            finally:
                relay.close()
                archive.close()

    def test_rejects_unprotected_secret_and_remote_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owners"
            seed.master_key(path, create=True)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                seed.master_key(path, create=False)
        for address in ("http://example.com", "https://a:b@example.com", "https://example.com/path"):
            with self.assertRaises(ValueError):
                seed.endpoint(address)
        self.assertEqual(seed.endpoint("http://127.0.0.1:8787/"), "http://127.0.0.1:8787")


if __name__ == "__main__":
    unittest.main()
