import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class ManifestTests(unittest.TestCase):
    def test_app_options_defaults_and_schema_are_exactly_aligned(self):
        config = yaml.safe_load((ROOT / "ipv6_proxy_monitor/config.yaml").read_text())
        self.assertEqual(list(config["options"]), list(config["schema"]))
        self.assertEqual(config["options"], {"hostname": "", "cidr": 128,
                                             "recheck_interval": 5})
        self.assertEqual(config["schema"]["cidr"], "int(1,128)")
        self.assertEqual(config["schema"]["recheck_interval"], "int(1,1440)")
        self.assertEqual(config["arch"], ["amd64", "aarch64"])
        self.assertEqual(config["homeassistant"], "2026.10.0")
        self.assertEqual(config["stage"], "experimental")
        self.assertTrue(config["init"])
        self.assertTrue(config["homeassistant_api"])
        self.assertTrue(config["ingress"])
        self.assertNotIn("ports", config)
        self.assertNotIn("map", config)
        self.assertNotIn("image", config)

    def test_versions_repository_manifest_and_pinned_multiplatform_base(self):
        config = yaml.safe_load((ROOT / "ipv6_proxy_monitor/config.yaml").read_text())
        self.assertEqual((ROOT / "VERSION").read_text().strip(), config["version"])
        repo = yaml.safe_load((ROOT / "repository.yaml").read_text())
        self.assertEqual(repo["url"], "https://github.com/matrix2669/home-assistant-addons")
        dockerfile = (ROOT / "ipv6_proxy_monitor/Dockerfile").read_text()
        self.assertIn("python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2", dockerfile)
        self.assertIn('CMD ["python", "-m", "ipv6_proxy_monitor.run"]', dockerfile)


if __name__ == "__main__":
    unittest.main()
