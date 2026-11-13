import copy
import tempfile
import unittest
from pathlib import Path

from edge_config import bootstrap_config, seal_bootstrap
from release_state import Stop, canonical
from routectl import BLUE_PAIR, RouteConfig
from test_routectl import edge_config


class EdgeBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.adapted = edge_config()
        del self.adapted["apps"]["http"]["servers"]["public"]["@id"]
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base_dir = Path(temp.name)
        self.config_dir = self.base_dir / "config"
        self.config_dir.mkdir(mode=0o700)
        self.baseline = self.base_dir / "sealed.json"

    def test_bootstrap_seals_matching_generation_zero(self):
        config = bootstrap_config(canonical(self.adapted))
        self.assertEqual(config.active, BLUE_PAIR)
        self.assertEqual(config.generation, 0)
        seal_bootstrap(config, self.config_dir, self.baseline)
        self.assertEqual(self.baseline.read_bytes(), canonical(config.config))
        self.assertEqual(
            (self.config_dir / "caddy" / "autosave.json").read_bytes(), self.baseline.read_bytes()
        )

    def test_bootstrap_rejects_an_existing_generation(self):
        with self.assertRaisesRegex(Stop, "UNEXPECTED_BOOT_GENERATION"):
            bootstrap_config(canonical(edge_config()))

    def test_bootstrap_rejects_green_active(self):
        initial = edge_config()
        green = RouteConfig(initial).changed({"api": "GREEN", "frontend": "GREEN"}, 0).config
        del green["apps"]["http"]["servers"]["public"]["@id"]
        with self.assertRaisesRegex(Stop, "BOOTSTRAP_REQUIRES_BLUE"):
            bootstrap_config(canonical(green))

    def test_bootstrap_rejects_wildcard_preview_or_tcp_admin(self):
        for change, code in (
            ("listener", "PREVIEW_LISTENER_NOT_PRIVATE"),
            ("admin", "EDGE_ADMIN_NOT_PRIVATE"),
        ):
            with self.subTest(change=change):
                config = copy.deepcopy(self.adapted)
                if change == "listener":
                    config["apps"]["http"]["servers"]["preview-green"]["listen"] = [":8443"]
                else:
                    config["admin"]["listen"] = ":2019"
                with self.assertRaisesRegex(Stop, code):
                    bootstrap_config(canonical(config))

    def test_bootstrap_rejects_unexpected_upstream_or_duplicate_host(self):
        public = self.adapted["apps"]["http"]["servers"]["public"]
        public["routes"].append(copy.deepcopy(public["routes"][0]))
        with self.assertRaisesRegex(Stop, "EDGE_HOST_OR_SNI_POLICY_CHANGED"):
            bootstrap_config(canonical(self.adapted))
        public["routes"].pop()
        api_handlers = public["routes"][0]["handle"][0]["routes"][0]["handle"]
        api_handlers[1]["upstreams"][0]["dial"] = "backend:8000"
        with self.assertRaisesRegex(Stop, "UNKNOWN_PUBLIC_UPSTREAM"):
            bootstrap_config(canonical(self.adapted))

    def test_bootstrap_never_overwrites_existing_autosave(self):
        config = bootstrap_config(canonical(self.adapted))
        autosave_dir = self.config_dir / "caddy"
        autosave_dir.mkdir(mode=0o700)
        autosave = autosave_dir / "autosave.json"
        autosave.write_bytes(b"existing-generation")
        with self.assertRaisesRegex(Stop, "BOOTSTRAP_ARTIFACT_EXISTS"):
            seal_bootstrap(config, self.config_dir, self.baseline)
        self.assertFalse(self.baseline.exists())
        self.assertEqual(autosave.read_bytes(), b"existing-generation")

    def test_bootstrap_never_overwrites_existing_baseline(self):
        config = bootstrap_config(canonical(self.adapted))
        self.baseline.write_bytes(b"existing-baseline")
        with self.assertRaisesRegex(Stop, "BOOTSTRAP_ARTIFACT_EXISTS"):
            seal_bootstrap(config, self.config_dir, self.baseline)
        self.assertFalse((self.config_dir / "caddy" / "autosave.json").exists())
        self.assertEqual(self.baseline.read_bytes(), b"existing-baseline")
