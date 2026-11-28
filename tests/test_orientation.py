"""Focused checks for request-time orientation and profile references."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from core.api_provision import ChatMessage
from core.orientation import orientation_system_message, orient_messages
from core.profiles import load_profiles


class OrientationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = {
            "timestamp": 123.0,
            "host": {"hostname": "private-host", "system": "Linux", "release": "test",
                     "architecture": "x86_64", "processor": "test CPU",
                     "logical_cpus": 8, "memory_total_mb": 4096,
                     "working_directory_disk": {"total_gb": 10, "free_gb": 5}},
            "network": {
                "interfaces": {"available": True, "interfaces": [{"name": "eth0", "state": "UP",
                    "addresses": [{"address": "192.168.1.4", "family": "inet", "scope": "global"}]}]},
                "default_routes": {"available": True, "routes": [{"gateway": "192.168.1.1", "dev": "eth0"}]},
            },
            "gpu": {"available": True, "devices": [{"vendor": "Test", "name": "GPU",
                "temperature_c": 40, "gpu_util_pct": 10, "memory_used_mb": 100,
                "memory_total_mb": 1000}]},
        }

    def test_model_orientation_redacts_host_and_network_identifiers(self) -> None:
        with patch("core.orientation.collect_orientation", return_value=self.snapshot):
            prompt = orientation_system_message({"id": "arko", "name": "Arko", "role": "owner"})
        self.assertNotIn("private-host", prompt)
        self.assertNotIn("192.168.1.4", prompt)
        self.assertNotIn("192.168.1.1", prompt)
        self.assertIn("not authenticated", prompt)
        self.assertIn("x86_64", prompt)

    def test_orientation_is_added_without_mutating_original_messages(self) -> None:
        original = [ChatMessage("system", "existing rules"), ChatMessage("user", "hello")]
        with patch("core.orientation.collect_orientation", return_value=self.snapshot):
            sent = orient_messages(original)
        self.assertEqual(len(sent), 2)
        self.assertIn("existing rules", sent[0].content)
        self.assertIn("spatial_context", sent[0].content)
        self.assertNotIn("spatial_context", original[0].content)

    def test_profiles_have_unique_ids_and_https_work_links(self) -> None:
        profiles = load_profiles()
        ids = [profile["id"] for profile in profiles]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(profiles), 10)
        for profile in profiles:
            self.assertTrue(profile["works"])
            self.assertTrue(all(work["url"].startswith("https://") for work in profile["works"]))


if __name__ == "__main__":
    unittest.main()
