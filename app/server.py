#!/usr/bin/env python3
"""
Hundjäger Media Hub - Local Web GUI & Automation Server
Zero-external-dependency local HTTP server providing stream format inspection,
threat-modeled security defenses, and real-time download streaming with yt-dlp.
"""

import os
import sys
import json
import time
import re
import subprocess
import threading
import uuid
import platform
import argparse
import ipaddress
import shutil
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

DEFAULT_PORT = 5050
DEFAULT_HOST = "127.0.0.1"
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DOWNLOADS_DIR = Path.home() / "Downloads"
MAX_TASKS = 100

# Server runtime state
PORT = DEFAULT_PORT
HOST = DEFAULT_HOST
ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost"}

# Find yt-dlp executable
def find_yt_dlp():
    candidates = [
        "/opt/homebrew/bin/yt-dlp",
        "/usr/local/bin/yt-dlp",
        str(Path.home() / ".local/bin/yt-dlp"),
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return shutil.which("yt-dlp") or "yt-dlp"

YT_DLP_BIN = find_yt_dlp()

# Task store for tracking live downloads
TASKS = {}
TASKS_LOCK = threading.Lock()


def format_size(bytes_val, is_approx=False):
    if not bytes_val or bytes_val <= 0:
        return "Unknown size"
    prefix = "~" if is_approx else ""
    val = float(bytes_val)
    for unit in ['B', 'KB', 'MB', 'GB']:
        if val < 1024.0:
            return f"{prefix}{val:.1f} {unit}"
        val /= 1024.0
    return f"{prefix}{val:.1f} TB"


def estimate_stream_bytes(f, duration=None, height=None):
    """
    Determines or estimates the stream size in bytes.
    Returns (bytes_int, is_approx_bool).
    """
    fs = f.get("filesize")
    if fs and fs > 0:
        return int(fs), False

    fsa = f.get("filesize_approx")
    if fsa and fsa > 0:
        return int(fsa), True

    dur = duration if (duration and duration > 0) else 0
    if dur > 0:
        tbr = f.get("tbr")
        if tbr and tbr > 0:
            return int((tbr * 1000 / 8) * dur), True

        vbr = f.get("vbr")
        if vbr and vbr > 0:
            abr = f.get("abr") or 128
            return int(((vbr + abr) * 1000 / 8) * dur), True

        abr = f.get("abr")
        if abr and abr > 0 and (f.get("vcodec") == "none" or not height):
            return int((abr * 1000 / 8) * dur), True

        h = height or f.get("height")
        if h:
            profile_kbps = {
                4320: 35000,
                2160: 18000,
                1440: 8000,
                1080: 3200,
                720: 1500,
                480: 800,
                360: 500,
                240: 280,
                144: 140
            }
            closest_h = min(profile_kbps.keys(), key=lambda k: abs(k - h))
            kbps = profile_kbps[closest_h]
            return int((kbps * 1000 / 8) * dur), True

    return 0, False


def format_duration(seconds):
    if not seconds:
        return "Unknown duration"
    try:
        seconds = int(seconds)
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"
    except Exception:
        return str(seconds)


def clean_input_url(raw_url):
    """Extracts first valid HTTP/HTTPS URL from messy input."""
    m = re.search(r'https?://[^\s|]+', raw_url)
    url = m.group(0) if m else raw_url.strip()
    return url.rstrip('/\\')


def build_format_selector(format_id=None, resolution=None):
    """
    Constructs a resilient format selector string for yt-dlp.
    Deduplicated helper across download, retry, and retry-all operations.
    """
    if format_id and format_id.startswith("http"):
        return format_id
    if resolution == "Audio Only" or format_id == "bestaudio/best":
        return "bestaudio/best"
    if resolution and resolution.endswith("p"):
        h = resolution.replace("p", "")
        if format_id and format_id != "best" and not format_id.startswith("http"):
            return f"{format_id}/bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
        return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
    if format_id and format_id != "best":
        return f"{format_id}/best"
    return "best"


def open_in_file_manager(folder_path):
    """Platform-aware opener for the local downloads directory."""
    sys_name = platform.system()
    try:
        if sys_name == "Darwin":
            subprocess.Popen(["open", str(folder_path)])
        elif sys_name == "Windows":
            os.startfile(str(folder_path))
        else:  # Linux / BSD
            subprocess.Popen(["xdg-open", str(folder_path)])
        return True, None
    except Exception as exc:
        return False, str(exc)


def prune_tasks_locked():
    """
    Enforces LRU bounded task storage.
    Evicts oldest finished, cancelled, or error tasks if total exceeds MAX_TASKS.
    """
    if len(TASKS) <= MAX_TASKS:
        return
    candidates = [
        (tid, t.get("created_at", 0))
        for tid, t in TASKS.items()
        if t.get("status") in ("finished", "cancelled", "error")
    ]
    candidates.sort(key=lambda x: x[1])
    to_evict = len(TASKS) - MAX_TASKS
    for tid, _ in candidates[:to_evict]:
        del TASKS[tid]


def inspect_url(url):
    """Runs yt-dlp in dump-json mode to extract metadata and formats."""
    url = clean_input_url(url)
    cmd = [
        YT_DLP_BIN,
        "-j",
        "--skip-download",
        "--socket-timeout", "30",
        "--no-playlist",
        url
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if proc.returncode != 0:
            err = proc.stderr.strip() or "Failed to inspect URL"
            return {"success": False, "error": err}
        
        data = json.loads(proc.stdout)
        title = data.get("title", "Untitled Video")
        thumbnail = data.get("thumbnail")
        duration = data.get("duration")
        duration_str = data.get("duration_string") or format_duration(duration)
        extractor = data.get("extractor_key") or data.get("extractor", "Generic")
        raw_formats = data.get("formats", [])

        video_formats = {}
        has_audio = False

        best_audio_bytes = 0
        for f in raw_formats:
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            if vcodec == "none" and acodec != "none":
                has_audio = True
                a_bytes, _ = estimate_stream_bytes(f, duration)
                if a_bytes > best_audio_bytes:
                    best_audio_bytes = a_bytes

        if has_audio and best_audio_bytes == 0 and duration:
            best_audio_bytes = int((128 * 1000 / 8) * duration)

        for f in raw_formats:
            fmt_id = str(f.get("format_id", ""))
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            height = f.get("height")
            ext = f.get("ext", "mp4")

            if acodec != "none":
                has_audio = True

            if vcodec != "none" and height:
                res_key = f"{height}p"
                f_bytes, is_approx = estimate_stream_bytes(f, duration, height)

                if acodec == "none" and f_bytes > 0:
                    f_bytes += best_audio_bytes
                    is_approx = True

                current_best = video_formats.get(res_key)
                if not current_best or f_bytes > (current_best.get("filesize") or 0):
                    video_formats[res_key] = {
                        "format_id": fmt_id,
                        "label": res_key,
                        "height": height,
                        "ext": ext,
                        "filesize": f_bytes,
                        "filesize_str": format_size(f_bytes, is_approx),
                        "protocol": f.get("protocol", "https")
                    }

        sorted_resolutions = sorted(
            video_formats.values(),
            key=lambda x: x["height"],
            reverse=True
        )

        audio_size_bytes = best_audio_bytes if best_audio_bytes > 0 else (int((128 * 1000 / 8) * duration) if duration else 0)
        audio_size_str = format_size(audio_size_bytes, is_approx=True) if audio_size_bytes > 0 else "Smallest Size"

        return {
            "success": True,
            "title": title,
            "thumbnail": thumbnail,
            "duration": duration_str,
            "duration_seconds": duration,
            "extractor": extractor,
            "resolutions": sorted_resolutions,
            "audio_size": audio_size_str,
            "url": url
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Inspection timed out after 45 seconds"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def is_valid_metric(val):
    if not val:
        return False
    v = str(val).strip().upper()
    return v not in ("", "--", "N/A", "NA", "UNKNOWN", "NONE")


def run_download_task(task_id, url, format_selector, output_dir, title=None):
    """Worker thread running yt-dlp download with live stdout parsing."""
    cmd = [
        YT_DLP_BIN,
        "-P", str(output_dir),
        "--socket-timeout", "20",
        "--fragment-retries", "50",
        "--retries", "30",
        "--retry-sleep", "fragment:exp=1:20",
        "--abort-on-unavailable-fragments",
        "-c",
        "--newline",
        "--progress-template", "PROGRESS:%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s|%(progress._downloaded_bytes_str)s|%(progress._total_bytes_str)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s",
    ]

    if format_selector and format_selector.startswith("http"):
        download_target = format_selector
        if title:
            safe_title = "".join(c for c in title if c.isalnum() or c in " ._-").strip()
            if safe_title:
                cmd.extend(["-o", f"{safe_title}.%(ext)s"])
    else:
        download_target = url
        if format_selector and format_selector != "best":
            cmd.extend(["-f", format_selector])

    cmd.append(download_target)

    try:
        with TASKS_LOCK:
            # Atomicity rule: a task is never observable in state "downloading"
            # without its process handle stored, and a cancel decided while this
            # worker was starting always wins. Holding TASKS_LOCK across Popen
            # (fork + exec, a few ms) closes the /api/cancel no-op window.
            if TASKS[task_id].get("status") == "cancelled":
                return
            TASKS[task_id]["status"] = "downloading"
            TASKS[task_id]["started_at"] = time.time()
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            TASKS[task_id]["process"] = proc

        custom_progress_re = re.compile(
            r"^PROGRESS:\s*([0-9\.]+)%\s*\|\s*([^|]*)\|\s*([^|]*)\|\s*([^|]*)\|\s*([^|]*)(?:\|\s*([^|]*)\|\s*([^|\n]*))?"
        )
        fallback_progress_re = re.compile(r"\[download\]\s+([0-9\.]+)%\s+of\s+~?\s*([0-9\.]+\w+)\s+at\s+([0-9\.]+\w+/s)\s+ETA\s+([0-9\:]+)")
        dest_re = re.compile(r"\[download\] Destination:\s*(.+)")

        recent_output_lines = []
        for line in proc.stdout:
            line_str = line.strip()
            if line_str:
                recent_output_lines.append(line_str)
                if len(recent_output_lines) > 30:
                    recent_output_lines.pop(0)

            dest_match = dest_re.search(line_str)
            if dest_match:
                filename = os.path.basename(dest_match.group(1).strip())
                with TASKS_LOCK:
                    TASKS[task_id]["filename"] = filename

            m = custom_progress_re.search(line_str)
            if m:
                percent = float(m.group(1))
                speed = m.group(2).strip()
                eta = m.group(3).strip()
                downloaded_str = m.group(4).strip()
                total_str = m.group(5).strip()
                dl_bytes_raw = (m.group(6) or "").strip()
                tot_bytes_raw = (m.group(7) or "").strip()

                with TASKS_LOCK:
                    TASKS[task_id]["percent"] = percent
                    if is_valid_metric(speed):
                        TASKS[task_id]["speed"] = speed
                    if is_valid_metric(eta):
                        TASKS[task_id]["eta"] = eta

                    if is_valid_metric(downloaded_str):
                        TASKS[task_id]["downloaded"] = downloaded_str
                    elif dl_bytes_raw.isdigit() and int(dl_bytes_raw) > 0:
                        TASKS[task_id]["downloaded"] = format_size(int(dl_bytes_raw))

                    if is_valid_metric(total_str):
                        TASKS[task_id]["total"] = total_str
                    elif tot_bytes_raw.isdigit() and int(tot_bytes_raw) > 0:
                        TASKS[task_id]["total"] = format_size(int(tot_bytes_raw))
                continue

            fb = fallback_progress_re.search(line_str)
            if fb:
                percent = float(fb.group(1))
                total_fb = fb.group(2).strip()
                speed_fb = fb.group(3).strip()
                eta_fb = fb.group(4).strip()
                with TASKS_LOCK:
                    TASKS[task_id]["percent"] = percent
                    if is_valid_metric(speed_fb):
                        TASKS[task_id]["speed"] = speed_fb
                    if is_valid_metric(eta_fb):
                        TASKS[task_id]["eta"] = eta_fb
                    if is_valid_metric(total_fb):
                        TASKS[task_id]["total"] = total_fb

        proc.wait()

        with TASKS_LOCK:
            # A cancel that already marked the task wins, regardless of the
            # child's exit code (SIGTERM may surface as 0 or nonzero).
            if proc.returncode == 0 and TASKS[task_id].get("status") != "cancelled":
                TASKS[task_id]["status"] = "finished"
                TASKS[task_id]["percent"] = 100.0
                TASKS[task_id]["eta"] = "00:00"
                TASKS[task_id]["speed"] = ""
                tot = TASKS[task_id].get("total") or TASKS[task_id].get("filesize_str")
                if is_valid_metric(tot):
                    clean_tot = tot.lstrip("~")
                    TASKS[task_id]["downloaded"] = clean_tot
                    TASKS[task_id]["total"] = clean_tot
            else:
                if TASKS[task_id].get("status") != "cancelled":
                    TASKS[task_id]["status"] = "error"
                    err_msg = ""
                    for oline in reversed(recent_output_lines):
                        if "ERROR:" in oline:
                            err_msg = oline.replace("ERROR:", "").strip()
                            break
                    if not err_msg:
                        for oline in reversed(recent_output_lines):
                            if "error" in oline.lower():
                                err_msg = oline
                                break
                    if not err_msg and recent_output_lines:
                        err_msg = recent_output_lines[-1]

                    if err_msg:
                        TASKS[task_id]["error"] = f"{err_msg} (code {proc.returncode})"
                    else:
                        TASKS[task_id]["error"] = f"Exited with code {proc.returncode}"
    except Exception as e:
        with TASKS_LOCK:
            TASKS[task_id]["status"] = "error"
            TASKS[task_id]["error"] = str(e)


class MediaHubHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def _is_request_authorized(self):
        """
        Threat-models and mitigates drive-by CSRF and DNS-rebinding attacks
        against the local server instance.
        """
        # 1. Host Header Validation (DNS Rebinding Defense)
        raw_host = self.headers.get("Host", "").strip()
        if not raw_host:
            return False, "Missing Host header"

        if ":" in raw_host:
            host_name, _, host_port = raw_host.partition(":")
        else:
            host_name, host_port = raw_host, ""

        if host_name.lower() not in ALLOWED_HOSTNAMES:
            return False, f"Invalid Host: {host_name}"

        if host_port and host_port != str(PORT):
            return False, f"Invalid Host port: {host_port}"

        # 2. Modern Browser Sec-Fetch-Site Check
        sec_fetch = self.headers.get("Sec-Fetch-Site", "").strip().lower()
        if sec_fetch == "cross-site":
            return False, "Cross-site request rejected"

        # 3. Origin Header Validation (CORS / Drive-by defense)
        origin = self.headers.get("Origin")
        if origin:
            try:
                parsed_orig = urlparse(origin)
                orig_host = parsed_orig.hostname
                orig_port = parsed_orig.port or (443 if parsed_orig.scheme == "https" else 80)
                if orig_host not in ALLOWED_HOSTNAMES or orig_port != PORT:
                    return False, f"Unauthorized origin: {origin}"
            except Exception:
                return False, "Malformed Origin header"

        return True, None

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # Only reflect Origin if it is verified local origin (no wildcard *)
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        auth, reason = self._is_request_authorized()
        if not auth:
            self._send_json({"success": False, "error": f"Forbidden: {reason}"}, 403)
            return

        self.send_response(204)
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        auth, reason = self._is_request_authorized()
        if not auth:
            self._send_json({"success": False, "error": f"Forbidden: {reason}"}, 403)
            return

        parsed = urlparse(self.path)

        # Enforce application/json for all API routes (blocks simple cross-origin requests)
        if parsed.path.startswith("/api/"):
            content_type = self.headers.get("Content-Type", "")
            if not content_type.startswith("application/json"):
                self._send_json({"success": False, "error": "Content-Type must be application/json"}, 415)
                return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            payload = {}

        if parsed.path == "/api/inspect":
            url = payload.get("url", "").strip()
            if not url:
                self._send_json({"success": False, "error": "URL is required"}, 400)
                return
            res = inspect_url(url)
            self._send_json(res)

        elif parsed.path == "/api/download":
            url = payload.get("url", "").strip()
            format_id = payload.get("format_id", "").strip()
            title = payload.get("title", "Video Download").strip()
            resolution = payload.get("resolution", "Default")

            if not url:
                self._send_json({"success": False, "error": "URL is required"}, 400)
                return

            task_id = str(uuid.uuid4())[:8]
            format_selector = build_format_selector(format_id, resolution)
            filesize_str = payload.get("filesize_str", "").strip()

            with TASKS_LOCK:
                prune_tasks_locked()
                TASKS[task_id] = {
                    "id": task_id,
                    "title": title,
                    "url": url,
                    "resolution": resolution,
                    "format_selector": format_selector,
                    "filesize_str": filesize_str,
                    "percent": 0.0,
                    "speed": "--",
                    "eta": "--",
                    "downloaded": "--",
                    "total": filesize_str if filesize_str else "--",
                    "status": "starting",
                    "filename": None,
                    "error": None,
                    "created_at": time.time()
                }

            t = threading.Thread(
                target=run_download_task,
                args=(task_id, url, format_selector, DOWNLOADS_DIR, title),
                daemon=True
            )
            t.start()

            self._send_json({"success": True, "task_id": task_id})

        elif parsed.path == "/api/cancel":
            task_id = payload.get("task_id")
            with TASKS_LOCK:
                task = TASKS.get(task_id)
                if task and task.get("status") not in ("finished", "error", "cancelled"):
                    # Cancel is decided under the same lock the worker uses to
                    # store its process handle / re-check "cancelled", so it
                    # cannot be lost or overwritten during a retry restart.
                    task["status"] = "cancelled"
                    if task.get("process"):
                        try:
                            task["process"].terminate()
                        except Exception:
                            pass
            self._send_json({"success": True})

        elif parsed.path == "/api/tasks/retry":
            task_id = payload.get("task_id")
            task_to_run = None
            with TASKS_LOCK:
                task = TASKS.get(task_id)
                if not task:
                    self._send_json({"success": False, "error": "Task not found"}, 404)
                    return

                if task.get("process"):
                    try:
                        task["process"].terminate()
                    except Exception:
                        pass

                task["status"] = "starting"
                task["error"] = None
                task["speed"] = "--"
                task["eta"] = "--"

                url = clean_input_url(task.get("url", ""))
                task["url"] = url
                format_selector = task.get("format_selector") or build_format_selector(
                    task.get("format_id"), task.get("resolution")
                )
                task["format_selector"] = format_selector
                title = task.get("title")
                task_to_run = (task_id, url, format_selector, DOWNLOADS_DIR, title)

            if task_to_run:
                t = threading.Thread(
                    target=run_download_task,
                    args=task_to_run,
                    daemon=True
                )
                t.start()

            self._send_json({"success": True})

        elif parsed.path == "/api/tasks/retry-all":
            tasks_to_run = []
            with TASKS_LOCK:
                for tid, task in TASKS.items():
                    if task.get("status") in ("error", "cancelled"):
                        if task.get("process"):
                            try:
                                task["process"].terminate()
                            except Exception:
                                pass

                        task["status"] = "starting"
                        task["error"] = None
                        task["speed"] = "--"
                        task["eta"] = "--"

                        url = clean_input_url(task.get("url", ""))
                        task["url"] = url
                        format_selector = task.get("format_selector") or build_format_selector(
                            task.get("format_id"), task.get("resolution")
                        )
                        task["format_selector"] = format_selector
                        title = task.get("title")
                        tasks_to_run.append((tid, url, format_selector, DOWNLOADS_DIR, title))

            for task_args in tasks_to_run:
                t = threading.Thread(
                    target=run_download_task,
                    args=task_args,
                    daemon=True
                )
                t.start()

            self._send_json({"success": True, "count": len(tasks_to_run)})

        elif parsed.path == "/api/tasks/delete":
            task_id = payload.get("task_id")
            with TASKS_LOCK:
                task = TASKS.get(task_id)
                if task:
                    if task.get("process"):
                        try:
                            task["process"].terminate()
                        except Exception:
                            pass
                    del TASKS[task_id]
            self._send_json({"success": True})

        elif parsed.path == "/api/tasks/clear":
            mode = payload.get("mode", "finished")
            with TASKS_LOCK:
                if mode == "all":
                    for t in TASKS.values():
                        if t.get("process"):
                            try:
                                t["process"].terminate()
                            except Exception:
                                pass
                    TASKS.clear()
                else:
                    to_remove = [k for k, v in TASKS.items() if v.get("status") in ("finished", "cancelled", "error")]
                    for k in to_remove:
                        del TASKS[k]
            self._send_json({"success": True})

        elif parsed.path in ("/api/open-downloads", "/api/open-folder"):
            ok, err = open_in_file_manager(DOWNLOADS_DIR)
            if ok:
                self._send_json({"success": True})
            else:
                self._send_json({"success": False, "error": err}, 500)

        else:
            self._send_json({"success": False, "error": "Endpoint not found"}, 404)

    def do_GET(self):
        auth, reason = self._is_request_authorized()
        if not auth:
            self._send_json({"success": False, "error": f"Forbidden: {reason}"}, 403)
            return

        parsed = urlparse(self.path)
        if parsed.path == "/api/tasks":
            with TASKS_LOCK:
                safe_tasks = []
                for t in TASKS.values():
                    copy_t = {k: v for k, v in t.items() if k != "process"}
                    safe_tasks.append(copy_t)
            self._send_json({"success": True, "tasks": safe_tasks})

        elif parsed.path == "/api/status":
            self._send_json({
                "status": "online",
                "yt_dlp": YT_DLP_BIN,
                "downloads_dir": str(DOWNLOADS_DIR)
            })

        else:
            super().do_GET()


def is_loopback_host(host):
    """True for loopback IPs (::1, 127.0.0.0/8) and the 'localhost' name."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def resolve_allowed_hostnames(host, allow_hosts_arg):
    """
    Builds the Host-header allowlist. Loopback-only by default; extra Host
    names require explicit --allow-hosts opt-in. Returns (allowed_set, warning).
    """
    allowed = {"127.0.0.1", "localhost"}
    for h in (allow_hosts_arg or "").split(","):
        if h.strip():
            allowed.add(h.strip())

    warning = None
    if not is_loopback_host(host):
        if host in allowed:
            warning = (
                f"⚠️  Exposed beyond loopback: Host '{host}' was explicitly "
                f"allowed via --allow-hosts. Only expose to trusted networks."
            )
        else:
            warning = (
                f"⚠️  Binding non-loopback ({host}) without opt-in: clients "
                f"sending Host '{host}:<port>' will be rejected (403). "
                f"Pass --allow-hosts \"{host}\" to allow them explicitly."
            )
    return allowed, warning


def main():
    global HOST, PORT, DOWNLOADS_DIR, ALLOWED_HOSTNAMES

    parser = argparse.ArgumentParser(description="Hundjäger Media Hub - Apex Stream Extractor")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Binding host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Binding port (default: 5050)")
    parser.add_argument("--downloads-dir", default=str(DOWNLOADS_DIR), help="Path to destination downloads folder")
    parser.add_argument(
        "--allow-hosts", default="",
        help="Comma-separated Host header names to permit when exposing beyond loopback (never populated automatically)"
    )
    args = parser.parse_args()

    HOST = args.host
    PORT = args.port
    DOWNLOADS_DIR = Path(args.downloads_dir).resolve()
    ALLOWED_HOSTNAMES, allowlist_warning = resolve_allowed_hostnames(HOST, args.allow_hosts)

    print("==================================================")
    print("🚀 Hundjäger Media Hub Server starting...")
    print(f"📍 Local Interface: http://{HOST}:{PORT}")
    print(f"⚙️  yt-dlp Binary: {YT_DLP_BIN}")
    print(f"📁 Downloads Path: {DOWNLOADS_DIR}")
    print("🛡️  Threat Model: Host/Origin validation active")
    if allowlist_warning:
        print(allowlist_warning)
    print("==================================================")

    server = ThreadingHTTPServer((HOST, PORT), MediaHubHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Hundjäger Media Hub Server...")
        server.shutdown()


if __name__ == "__main__":
    main()
