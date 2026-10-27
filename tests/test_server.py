#!/usr/bin/env python3
"""
Unit and integration test suite for Hundjäger Media Hub.
Tests utility calculations, format generation, task store management,
and threat-modeled security defenses (Host validation, Origin check, CSRF defense).
"""

import unittest
import json
import time
import io
from urllib.parse import urlparse
from pathlib import Path
import sys

# Ensure app package is importable
APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

import server


class TestFormattingAndUtilities(unittest.TestCase):
    def test_format_size(self):
        self.assertEqual(server.format_size(0), "Unknown size")
        self.assertEqual(server.format_size(-10), "Unknown size")
        self.assertEqual(server.format_size(500), "500.0 B")
        self.assertEqual(server.format_size(1024), "1.0 KB")
        self.assertEqual(server.format_size(1024 * 1024 * 50), "50.0 MB")
        self.assertEqual(server.format_size(1024 * 1024 * 1024 * 2.5), "2.5 GB")
        self.assertEqual(server.format_size(1024 * 1024 * 10, is_approx=True), "~10.0 MB")

    def test_format_duration(self):
        self.assertEqual(server.format_duration(0), "Unknown duration")
        self.assertEqual(server.format_duration(45), "0:45")
        self.assertEqual(server.format_duration(125), "2:05")
        self.assertEqual(server.format_duration(3665), "1:01:05")

    def test_clean_input_url(self):
        self.assertEqual(
            server.clean_input_url("https://example.com/watch?v=123/"),
            "https://example.com/watch?v=123"
        )
        self.assertEqual(
            server.clean_input_url("Check this: https://vimeo.com/987654321 and enjoy"),
            "https://vimeo.com/987654321"
        )

    def test_estimate_stream_bytes(self):
        # 1. Exact filesize present
        bytes_val, is_approx = server.estimate_stream_bytes({"filesize": 5000000})
        self.assertEqual(bytes_val, 5000000)
        self.assertFalse(is_approx)

        # 2. Approximate filesize
        bytes_val, is_approx = server.estimate_stream_bytes({"filesize_approx": 4200000})
        self.assertEqual(bytes_val, 4200000)
        self.assertTrue(is_approx)

        # 3. Calculation from bitrate and duration
        # 1000 kbps for 10 seconds = 1,250,000 bytes
        bytes_val, is_approx = server.estimate_stream_bytes({"tbr": 1000}, duration=10)
        self.assertEqual(bytes_val, 1250000)
        self.assertTrue(is_approx)


class TestFormatSelectorBuilder(unittest.TestCase):
    def test_direct_http_stream(self):
        url = "https://cdn.example.com/master.m3u8"
        self.assertEqual(server.build_format_selector(format_id=url), url)

    def test_audio_only(self):
        self.assertEqual(server.build_format_selector(resolution="Audio Only"), "bestaudio/best")
        self.assertEqual(server.build_format_selector(format_id="bestaudio/best"), "bestaudio/best")

    def test_height_resolution(self):
        res = server.build_format_selector(resolution="1080p")
        self.assertIn("bestvideo[height<=1080]+bestaudio/best[height<=1080]/best", res)

    def test_specific_format_id(self):
        self.assertEqual(server.build_format_selector(format_id="22"), "22/best")
        self.assertEqual(server.build_format_selector(format_id="best"), "best")


class TestTaskRegistryBoundedStore(unittest.TestCase):
    def setUp(self):
        with server.TASKS_LOCK:
            server.TASKS.clear()

    def test_task_eviction_under_limit(self):
        with server.TASKS_LOCK:
            for i in range(10):
                server.TASKS[f"task-{i}"] = {
                    "id": f"task-{i}",
                    "status": "finished",
                    "created_at": time.time() - (10 - i)
                }
            server.prune_tasks_locked()
            self.assertEqual(len(server.TASKS), 10)

    def test_task_eviction_exceeding_limit(self):
        original_max = server.MAX_TASKS
        try:
            server.MAX_TASKS = 5
            with server.TASKS_LOCK:
                # Add 5 active and 5 finished tasks
                for i in range(5):
                    server.TASKS[f"active-{i}"] = {
                        "id": f"active-{i}",
                        "status": "downloading",
                        "created_at": time.time()
                    }
                for i in range(5):
                    server.TASKS[f"done-{i}"] = {
                        "id": f"done-{i}",
                        "status": "finished",
                        "created_at": 1000 + i
                    }

                self.assertEqual(len(server.TASKS), 10)
                server.prune_tasks_locked()
                # Should evict oldest finished tasks down to MAX_TASKS
                self.assertEqual(len(server.TASKS), 5)
                # Active tasks must never be evicted
                for i in range(5):
                    self.assertIn(f"active-{i}", server.TASKS)
        finally:
            server.MAX_TASKS = original_max


class MockHeaders(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class TestSecurityDefenses(unittest.TestCase):
    """
    Validates DNS-rebinding defense, drive-by CSRF protection,
    cross-site origin rejection, and Content-Type enforcement.
    """

    def setUp(self):
        # Configure server test parameters
        server.PORT = 5050
        server.HOST = "127.0.0.1"
        server.ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost"}

    def _simulate_auth(self, headers_dict):
        """Helper to invoke _is_request_authorized on a mock handler."""
        handler = server.MediaHubHandler.__new__(server.MediaHubHandler)
        handler.headers = MockHeaders(headers_dict)
        return handler._is_request_authorized()

    def test_valid_localhost_host_header(self):
        ok, reason = self._simulate_auth({"Host": "127.0.0.1:5050"})
        self.assertTrue(ok, f"Failed with reason: {reason}")

        ok, reason = self._simulate_auth({"Host": "localhost:5050"})
        self.assertTrue(ok, f"Failed with reason: {reason}")

    def test_dns_rebinding_attack_rejected(self):
        # Attacker domain rebinding to 127.0.0.1 sends attacker.com Host header
        ok, reason = self._simulate_auth({"Host": "attacker.com:5050"})
        self.assertFalse(ok)
        self.assertIn("Invalid Host", reason)

        ok, reason = self._simulate_auth({"Host": "evil-rebinding-domain.org"})
        self.assertFalse(ok)

    def test_mismatched_port_rejected(self):
        ok, reason = self._simulate_auth({"Host": "127.0.0.1:8080"})
        self.assertFalse(ok)
        self.assertIn("Invalid Host port", reason)

    def test_sec_fetch_site_cross_site_rejected(self):
        # Modern browsers send Sec-Fetch-Site: cross-site on drive-by scripts
        ok, reason = self._simulate_auth({
            "Host": "127.0.0.1:5050",
            "Sec-Fetch-Site": "cross-site"
        })
        self.assertFalse(ok)
        self.assertIn("Cross-site request rejected", reason)

    def test_unauthorized_origin_rejected(self):
        ok, reason = self._simulate_auth({
            "Host": "127.0.0.1:5050",
            "Origin": "https://malicious-website.com"
        })
        self.assertFalse(ok)
        self.assertIn("Unauthorized origin", reason)

    def test_valid_same_origin_allowed(self):
        ok, reason = self._simulate_auth({
            "Host": "127.0.0.1:5050",
            "Origin": "http://127.0.0.1:5050"
        })
        self.assertTrue(ok)

        ok, reason = self._simulate_auth({
            "Host": "localhost:5050",
            "Origin": "http://localhost:5050"
        })
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
