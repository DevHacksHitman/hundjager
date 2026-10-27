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
import subprocess
import sys
from unittest import mock
from types import SimpleNamespace
from urllib.parse import urlparse
from pathlib import Path

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


class TestAllowlistResolution(unittest.TestCase):
    """
    Validates that --allow-hosts is the only path to non-loopback Host names,
    and that binding non-loopback surfaces a startup warning (either rejected
    or explicitly permitted — never silently auto-allowed).
    """

    def test_loopback_default_allowlist(self):
        allowed, warning = server.resolve_allowed_hostnames("127.0.0.1", "")
        self.assertEqual(allowed, {"127.0.0.1", "localhost"})
        self.assertIsNone(warning)

    def test_localhost_and_ipv6_loopback(self):
        for host in ("localhost", "::1"):
            allowed, warning = server.resolve_allowed_hostnames(host, "")
            self.assertIsNone(warning)

    def test_bind_host_is_not_auto_whitelisted(self):
        # Regression: previously main() added HOST itself to the allowlist.
        allowed, warning = server.resolve_allowed_hostnames("0.0.0.0", "")
        self.assertNotIn("0.0.0.0", allowed)
        self.assertIn("403", warning)
        self.assertIn("--allow-hosts", warning)

    def test_explicit_opt_in_warns_about_exposure(self):
        allowed, warning = server.resolve_allowed_hostnames("0.0.0.0", "0.0.0.0")
        self.assertIn("0.0.0.0", allowed)
        self.assertIn("Exposed beyond loopback", warning)

    def test_extra_hosts_opt_in(self):
        allowed, warning = server.resolve_allowed_hostnames(
            "127.0.0.1", " myhost.example.com , 192.168.1.5")
        self.assertIn("myhost.example.com", allowed)
        self.assertIn("192.168.1.5", allowed)
        self.assertEqual(allowed, {"127.0.0.1", "localhost",
                                   "myhost.example.com", "192.168.1.5"})
        self.assertIsNone(warning)

    def test_public_name_treated_as_non_loopback(self):
        allowed, warning = server.resolve_allowed_hostnames("myhost.local", "")
        self.assertNotIn("myhost.local", allowed)
        self.assertIn("403", warning)


class FakeProc:
    """Minimal Popen stand-in for run_download_task tests."""
    def __init__(self, lines, returncode, on_wait=None):
        self.stdout = iter(lines)
        self.returncode = returncode
        self._on_wait = on_wait

    def wait(self):
        if self._on_wait:
            self._on_wait()
        return self.returncode

    def terminate(self):
        pass


class TestRunDownloadTaskSubprocess(unittest.TestCase):
    """
    Executes run_download_task against a fake yt-dlp child process to cover
    the PROGRESS: stdout parser, terminal transitions, and cancel races.
    """
    TASK_ID = "task-test"

    def setUp(self):
        with server.TASKS_LOCK:
            server.TASKS.clear()
            server.TASKS[self.TASK_ID] = {
                "id": self.TASK_ID,
                "title": "Task Test",
                "url": "https://example.com/watch?v=1",
                "resolution": "1080p",
                "format_selector": "22/best",
                "filesize_str": "",
                "percent": 0.0,
                "speed": "--",
                "eta": "--",
                "downloaded": "--",
                "total": "--",
                "status": "starting",
                "filename": None,
                "error": None,
                "created_at": time.time()
            }

    def tearDown(self):
        with server.TASKS_LOCK:
            server.TASKS.clear()

    def _spawn_worker(self, fake_proc, returncode=0):
        def popen_side_effect(*args, **kwargs):
            return fake_proc
        with mock.patch.object(server.subprocess, "Popen", side_effect=popen_side_effect):
            server.run_download_task(
                self.TASK_ID,
                "https://example.com/watch?v=1",
                "22/best",
                "/tmp/hundjager-test-downloads"
            )

    def test_progress_template_and_destination_parsing(self):
        proc = FakeProc(
            [
                "[download] Destination: /tmp/hundjager-test-downloads/Test Video [1].mp4",
                "PROGRESS: 50.0%|7.2MiB/s|00:30|5.2MiB|10.4MiB|5000000|10000000",
            ],
            returncode=0
        )
        self._spawn_worker(proc)

        with server.TASKS_LOCK:
            task = server.TASKS[self.TASK_ID]
            self.assertEqual(task["status"], "finished")
            self.assertEqual(task["filename"], "Test Video [1].mp4")
            self.assertEqual(task["percent"], 100.0)
            self.assertEqual(task["total"], "10.4MiB")
            self.assertEqual(task["downloaded"], "10.4MiB")

    def test_error_line_extraction(self):
        proc = FakeProc(
            [
                "[download] Destination: /tmp/hundjager-test-downloads/v.mp4",
                "second message line",
                "ERROR: Unsupported URL",
            ],
            returncode=1
        )
        self._spawn_worker(proc)

        with server.TASKS_LOCK:
            task = server.TASKS[self.TASK_ID]
            self.assertEqual(task["status"], "error")
            self.assertIn("Unsupported URL", task["error"])
            self.assertIn("(code 1)", task["error"])

    def test_cancel_status_sticky_when_exited_zero(self):
        # Simulates /api/cancel marking the task cancelled right before the
        # worker's terminal transition; a subsequent zero exit must not flip it.
        def mark_cancelled():
            with server.TASKS_LOCK:
                server.TASKS[self.TASK_ID]["status"] = "cancelled"

        proc = FakeProc([], returncode=0, on_wait=mark_cancelled)
        self._spawn_worker(proc)

        with server.TASKS_LOCK:
            self.assertEqual(server.TASKS[self.TASK_ID]["status"], "cancelled")

    def test_cancel_before_spawn_wins(self):
        # Task already cancelled during "starting": the worker must not spawn.
        with server.TASKS_LOCK:
            server.TASKS[self.TASK_ID]["status"] = "cancelled"

        popen_mock = mock.MagicMock()
        with mock.patch.object(server.subprocess, "Popen", popen_mock):
            server.run_download_task(
                self.TASK_ID, "https://example.com/watch?v=1", "22/best",
                "/tmp/hundjager-test-downloads"
            )
        popen_mock.assert_not_called()
        with server.TASKS_LOCK:
            self.assertEqual(server.TASKS[self.TASK_ID]["status"], "cancelled")

    def test_command_shape_for_format_selector(self):
        captured = {}

        def popen_side_effect(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            return FakeProc([], returncode=0)

        with mock.patch.object(server.subprocess, "Popen", side_effect=popen_side_effect):
            server.run_download_task(
                self.TASK_ID, "https://example.com/watch?v=1", "22/best",
                "/tmp/hundjager-test-downloads"
            )

        cmd = captured["cmd"]
        self.assertEqual(cmd[0], server.YT_DLP_BIN)
        self.assertEqual(cmd[1], "-P")
        self.assertEqual(cmd[2], "/tmp/hundjager-test-downloads")
        self.assertIn("-f", cmd)
        self.assertEqual(cmd[cmd.index("-f") + 1], "22/best")

    def test_best_selector_omits_format_flag(self):
        captured = {}

        def popen_side_effect(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            return FakeProc([], returncode=0)

        with mock.patch.object(server.subprocess, "Popen", side_effect=popen_side_effect):
            server.run_download_task(
                self.TASK_ID, "https://example.com/watch?v=1", "best",
                "/tmp/hundjager-test-downloads"
            )
        self.assertNotIn("-f", captured["cmd"])


class TestInspectUrlSubprocess(unittest.TestCase):
    """Covers inspect_url's yt-dlp -j invocation and format aggregation."""

    def _inspect(self, stdout="", stderr="", returncode=0, side_effect=None):
        def runner(cmd, **kwargs):
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        with mock.patch.object(server.subprocess, "run", side_effect=side_effect or runner) as fake_run:
            res = server.inspect_url("Messy text: https://example.com/watch?v=123/ and more")
            cmd = fake_run.call_args[0][0]
            return res, cmd

    def test_url_is_cleaned_before_invocation(self):
        res, cmd = self._inspect(stdout="{}")
        self.assertEqual(cmd[0], server.YT_DLP_BIN)
        self.assertEqual(cmd[1], "-j")
        self.assertIn("--skip-download", cmd)
        self.assertEqual(cmd[-1], "https://example.com/watch?v=123")

    def test_format_aggregation_and_sorting(self):
        payload = {
            "title": "Example Video",
            "duration": 100,
            "duration_string": "1:40",
            "thumbnail": "http://img/1",
            "extractor_key": "Example",
            "formats": [
                # 1080p: (3200*1000/8)*100 = 40,000,000 approx; audio-only 128 kbps -> 1,600,000
                {"format_id": "720p", "height": 720, "tbr": 1500, "vcodec": "avc1", "acodec": "none", "ext": "mp4"},
                {"format_id": "1080", "height": 1080, "tbr": 1000, "vcodec": "avc1", "acodec": "none", "ext": "mp4"},
                {"format_id": "1080p", "height": 1080, "tbr": 3200, "vcodec": "avc1", "acodec": "none", "ext": "mp4"},
                {"format_id": "m4a", "height": None, "tbr": 128, "vcodec": "none", "acodec": "mp4a", "ext": "m4a"},
            ],
        }
        res, _ = self._inspect(stdout=json.dumps(payload))
        self.assertTrue(res["success"])
        self.assertEqual(res["title"], "Example Video")
        self.assertEqual(res["duration"], "1:40")
        # Highest resolution first; best (largest) 1080p variant wins the dedup.
        self.assertEqual(res["resolutions"][0]["label"], "1080p")
        self.assertEqual(res["resolutions"][0]["format_id"], "1080p")
        # Video-only size plus the best audio estimate: 40,000,000 + 1,600,000.
        self.assertEqual(res["resolutions"][0]["filesize"], 41600000)
        self.assertEqual(res["resolutions"][1]["label"], "720p")
        self.assertEqual(res["audio_size"], "~1.5 MB")

    def test_failure_propagates_stderr(self):
        res, _ = self._inspect(returncode=1, stderr="ERROR: Unsupported URL")
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "ERROR: Unsupported URL")

    def test_timeout_message(self):
        res, _ = self._inspect(
            side_effect=subprocess.TimeoutExpired(cmd="yt-dlp-j", timeout=45))
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "Inspection timed out after 45 seconds")


if __name__ == "__main__":
    unittest.main()
