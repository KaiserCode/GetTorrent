#!/usr/bin/env python3
"""
GetTorrent with Proxy Rotation (Download-Only)
======================================================
A download-only torrent client that routes traffic through a user-supplied
list of proxies (ip:port). If a proxy fails or stops working mid-download,
it automatically switches to the next one. If every proxy is dead,
downloading stops immediately.

Features
--------
- Multiple simultaneous torrents shown in a numbered, multi-column list.
- Multi-select rows; sort by name / speed / status / progress / size / peers.
  Click a heading once to sort, click again to reverse. Index numbers are
  reassigned automatically to match the displayed order, and also update
  when torrents are added or removed.
- Live per-torrent download speed and a per-torrent progress bar drawn with
  a color gradient (red -> yellow -> green) plus a numeric percent.
- Click a torrent to see its files in a second numbered multi-column list.
- Each file shows a "piece map" bar: the bar is divided into segments and
  the segments that have actually been downloaded are shaded in-place, so
  if the middle of a file arrives first, the middle of the bar fills in
  (not a left-to-right thermometer).

Uploading / seeding / sharing of downloaded files is disabled entirely.

Requires:
    pip install libtorrent
    (tkinter ships with most Python installs; on Debian/Ubuntu:
     sudo apt install python3-tk)
"""

import os
import sys
import time
import threading
import queue
import itertools
import shutil
import subprocess
import re
import json
import socket
import tempfile
import base64
import hashlib
from urllib.parse import urlparse, parse_qs, unquote
import webbrowser

try:
    import libtorrent as lt
except ImportError:
    lt = None

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:
    pystray = None
    Image = None
    ImageDraw = None


# ---------------------------------------------------------------------------
# Packaged resource lookup
# ---------------------------------------------------------------------------
def resource_path(filename):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, filename)


# ---------------------------------------------------------------------------
# High-DPI awareness (helps on large / high-resolution monitors, e.g. a 50")
# ---------------------------------------------------------------------------
def enable_high_dpi():
    """Enable Windows DPI awareness and return a fallback DPI scale."""
    scale = 1.0
    if sys.platform == "win32":
        try:
            import ctypes
            # Per-monitor DPI awareness. PyInstaller process starts before Tk,
            # so this must happen before the root window is created.
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass

            # GetDpiForSystem is more useful than forcing a fake 96 DPI.
            user32 = ctypes.windll.user32
            if hasattr(user32, "GetDpiForSystem"):
                dpi = int(user32.GetDpiForSystem())
                if dpi > 0:
                    scale = dpi / 96.0
                    return scale

            hdc = user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)
            user32.ReleaseDC(0, hdc)
            if dpi > 0:
                scale = dpi / 96.0
        except Exception:
            scale = 1.0
    return scale


def window_dpi(root, fallback_scale=1.0):
    """Return the actual logical DPI Windows assigned to this Tk window."""
    fallback_dpi = max(96, int(round(96.0 * max(float(fallback_scale), 1.0))))
    if sys.platform != "win32":
        return fallback_dpi

    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = int(root.winfo_id())

        if hasattr(user32, "GetDpiForWindow"):
            dpi = int(user32.GetDpiForWindow(hwnd))
            if dpi > 0:
                return dpi

        if hasattr(user32, "GetDpiForSystem"):
            dpi = int(user32.GetDpiForSystem())
            if dpi > 0:
                return dpi
    except Exception:
        pass

    return fallback_dpi


def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024


def human_rate(bps):
    return human_size(bps) + "/s"


def format_eta_seconds(seconds):
    """Return ETA as HH:MM:SS; unknown/unbounded values show dashes."""
    try:
        seconds = float(seconds)
    except Exception:
        return "--:--:--"

    if seconds < 0 or seconds != seconds or seconds == float("inf"):
        return "--:--:--"

    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"



def gradient_color(frac):
    """Return a light neutral -> green progress background.

    100% uses the exact same grass/lime green as downloaded pieces in
    PieceMapBar (#33cc55), so a finished torrent is unmistakably complete.
    Red remains reserved for real failures.
    """
    frac = max(0.0, min(1.0, frac))

    if frac >= 0.999:
        return "#33cc55"

    start = (245, 245, 245)
    end = (214, 244, 220)
    r = int(start[0] + (end[0] - start[0]) * frac)
    g = int(start[1] + (end[1] - start[1]) * frac)
    b = int(start[2] + (end[2] - start[2]) * frac)
    return f"#{r:02x}{g:02x}{b:02x}"



def initial_display_name(source):
    """Return a friendly name before torrent metadata has arrived."""
    if source.lower().startswith("magnet:"):
        try:
            params = parse_qs(urlparse(source).query)
            display_name = params.get("dn", [""])[0].strip()
            if display_name:
                return display_name
        except Exception:
            pass
        return "Fetching metadata..."
    return os.path.basename(source) or source



def _normalize_btih(value):
    token = str(value or "").strip()
    if not token:
        return ""
    token = token.split(":")[-1].strip()
    if len(token) == 40 and all(c in "0123456789abcdefABCDEF" for c in token):
        return token.lower()
    if len(token) == 32:
        try:
            raw = base64.b32decode(token.upper(), casefold=True)
            if len(raw) == 20:
                return raw.hex()
        except Exception:
            pass
    return token.lower()


def torrent_identity_from_source(source):
    """Return a stable swarm identity without contacting the network."""
    source = str(source or "").strip()
    if not source:
        return ""

    if source.lower().startswith("magnet:"):
        try:
            params = parse_qs(urlparse(source).query)
            identities = []
            for raw_xt in params.get("xt", []):
                xt = str(raw_xt or "").strip()
                lower = xt.lower()
                if lower.startswith("urn:btih:"):
                    normalized = _normalize_btih(xt)
                    if normalized:
                        identities.append("btih:" + normalized)
                elif lower.startswith("urn:btmh:"):
                    identities.append("btmh:" + lower.split("urn:btmh:", 1)[1])
            if identities:
                return "|".join(sorted(set(identities)))
        except Exception:
            pass
        # Exact magnet fallback still prevents duplicate browser invocations.
        return "magnet:" + source.strip().lower()

    if source.lower().endswith(".torrent") and os.path.isfile(source) and lt is not None:
        try:
            ti = lt.torrent_info(source)
            identities = []

            try:
                ihs = ti.info_hashes()
                for attr, prefix in (("v1", "btih:"), ("v2", "btmh:1220")):
                    h = getattr(ihs, attr, None)
                    if h is None:
                        continue
                    value = str(h).strip().lower()
                    if value and set(value) != {"0"}:
                        if attr == "v1" and len(value) == 40:
                            identities.append(prefix + value)
                        elif attr == "v2" and len(value) == 64:
                            identities.append(prefix + value)
            except Exception:
                pass

            if not identities:
                try:
                    h = str(ti.info_hash()).strip().lower()
                    if h:
                        identities.append("btih:" + h)
                except Exception:
                    pass

            if identities:
                return "|".join(sorted(set(identities)))
        except Exception:
            pass

    try:
        return "file:" + os.path.normcase(os.path.abspath(source))
    except Exception:
        return "source:" + source.lower()


def safe_filename(name):
    """Make a Windows-safe filename."""
    name = (name or "torrent").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.rstrip(" .")
    return name or "torrent"


def system_downloads_folder():
    """Return the user's real Downloads known folder when possible."""
    if sys.platform == "win32":
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            downloads_guid = "{374DE290-123F-4565-9164-39C4925E467B}"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, downloads_guid)
            value = os.path.expandvars(value)
            if value:
                return os.path.abspath(value)
        except Exception:
            pass
    return os.path.abspath(os.path.join(os.path.expanduser("~"), "Downloads"))


def app_data_folder():
    """Return GetTorrent data folder, migrating the old app name once."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        new_path = os.path.join(base, "GetTorrent")
        old_path = os.path.join(base, "TorrentDownloader")
    else:
        base = os.path.join(os.path.expanduser("~"), ".config")
        new_path = os.path.join(base, "GetTorrent")
        old_path = os.path.join(base, "TorrentDownloader")

    # Preserve settings, queue/history, IPC state, and saved torrent metadata
    # from releases that used the old TorrentDownloader name.
    if not os.path.exists(new_path) and os.path.isdir(old_path):
        try:
            shutil.copytree(old_path, new_path)
        except Exception:
            pass

    return new_path

def settings_file_path():
    return os.path.join(app_data_folder(), "settings.json")


def load_app_settings():
    downloads = system_downloads_folder()
    settings = {
        "download_dir": downloads,
        "torrent_dir": os.path.join(downloads, "Torrent Files"),
        "use_proxy": False,
        "proxy_type": "Auto",
        "proxies": "",
        "main_tab": 0,
        "proxy_tab": 0,
        "queue_limit": 5,
        "window_state": "zoomed",
        "window_geometry": "",
        "theme": "Slate Night",
        "column_widths": {},
    }
    try:
        with open(settings_file_path(), "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for key in ("download_dir", "torrent_dir"):
                value = saved.get(key)
                if isinstance(value, str) and value.strip():
                    settings[key] = os.path.abspath(os.path.expandvars(value.strip()))

            settings["use_proxy"] = bool(saved.get("use_proxy", False))

            proxy_type = str(saved.get("proxy_type", "Auto"))
            if proxy_type in ("Auto", "HTTP", "SOCKS5"):
                settings["proxy_type"] = proxy_type

            proxies = saved.get("proxies", "")
            if isinstance(proxies, str):
                settings["proxies"] = proxies

            for key, maximum in (("main_tab", 2), ("proxy_tab", 1)):
                try:
                    value = int(saved.get(key, 0))
                    settings[key] = max(0, min(maximum, value))
                except Exception:
                    pass

            try:
                settings["queue_limit"] = max(
                    1, min(100, int(saved.get("queue_limit", 5)))
                )
            except Exception:
                settings["queue_limit"] = 5

            window_state = str(saved.get("window_state", "zoomed"))
            if window_state in ("normal", "zoomed"):
                settings["window_state"] = window_state

            geometry = str(saved.get("window_geometry", "") or "").strip()
            if re.match(r"^\d+x\d+[-+]\d+[-+]\d+$", geometry):
                settings["window_geometry"] = geometry

            theme = str(saved.get("theme", "Slate Night") or "").strip()
            if theme:
                settings["theme"] = theme

            saved_widths = saved.get("column_widths", {})
            if isinstance(saved_widths, dict):
                clean_widths = {}
                for key, value in saved_widths.items():
                    try:
                        width = int(value)
                    except Exception:
                        continue
                    if width > 0:
                        clean_widths[str(key)] = width
                settings["column_widths"] = clean_widths
    except Exception:
        pass
    return settings


def save_app_settings(settings):
    folder = app_data_folder()
    os.makedirs(folder, exist_ok=True)
    path = settings_file_path()
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    os.replace(temp, path)


def torrent_queue_file_path():
    return os.path.join(app_data_folder(), "torrents.json")


def torrent_queue_backup_path():
    return torrent_queue_file_path() + ".bak"


def _read_torrent_queue_file(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("Torrent queue file is not a JSON list.")
    return [entry for entry in data if isinstance(entry, dict)]


def load_torrent_queue():
    path = torrent_queue_file_path()
    backup = torrent_queue_backup_path()

    try:
        return _read_torrent_queue_file(path)
    except FileNotFoundError:
        pass
    except Exception as e:
        log_external_error(
            "Primary torrent history file could not be read.",
            exc=e,
            details=f"path={path!r}",
        )

    try:
        records = _read_torrent_queue_file(backup)
        if records:
            try:
                folder = app_data_folder()
                os.makedirs(folder, exist_ok=True)
                temp = path + ".restore.tmp"
                with open(temp, "w", encoding="utf-8") as f:
                    json.dump(records, f, indent=2)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass
                os.replace(temp, path)
            except Exception as e:
                log_external_error(
                    "Recovered torrent history from backup but could not "
                    "repair the primary history file.",
                    exc=e,
                    details=f"primary={path!r} backup={backup!r}",
                )
        return records
    except FileNotFoundError:
        return []
    except Exception as e:
        log_external_error(
            "Torrent history backup could not be read.",
            exc=e,
            details=f"path={backup!r}",
        )
        return []


def save_torrent_queue(entries):
    folder = app_data_folder()
    os.makedirs(folder, exist_ok=True)
    path = torrent_queue_file_path()
    backup = torrent_queue_backup_path()
    temp = path + ".tmp"

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass

    _read_torrent_queue_file(temp)

    if os.path.isfile(path):
        try:
            _read_torrent_queue_file(path)
            shutil.copy2(path, backup)
        except Exception:
            pass

    os.replace(temp, path)


_SINGLE_INSTANCE_HANDLE = None
_MUTEX_NAME = r"Local\GetTorrent_SingleInstance"


def acquire_single_instance():
    """Return True only for the first running copy of the Windows app."""
    global _SINGLE_INSTANCE_HANDLE
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if not handle:
            try:
                log_startup_diagnostic(
                    "CreateMutexW returned no handle; continuing as primary."
                )
            except Exception:
                pass
            return True
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _SINGLE_INSTANCE_HANDLE = handle
        return True
    except Exception as e:
        try:
            log_external_error(
                "Single-instance mutex check failed.",
                exc=e,
            )
        except Exception:
            pass
        return True


def release_single_instance():
    global _SINGLE_INSTANCE_HANDLE
    if _SINGLE_INSTANCE_HANDLE and sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(_SINGLE_INSTANCE_HANDLE)
        except Exception:
            pass
        _SINGLE_INSTANCE_HANDLE = None


def ipc_folder():
    return os.path.join(app_data_folder(), "ipc")


def clear_stale_ipc_messages(before_ns):
    folder = ipc_folder()
    os.makedirs(folder, exist_ok=True)
    for name in os.listdir(folder):
        if not name.endswith(".msg"):
            continue
        path = os.path.join(folder, name)
        try:
            if os.stat(path).st_mtime_ns < before_ns:
                os.remove(path)
        except Exception:
            pass


def external_error_log_path():
    folder = app_data_folder()
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "external-link-errors.log")


def startup_diagnostic_log_path():
    folder = app_data_folder()
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "startup-diagnostics.log")


def log_startup_diagnostic(message):
    """Always-on launch tracing for file/protocol association debugging."""
    try:
        path = startup_diagnostic_log_path()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        frozen = bool(getattr(sys, "frozen", False))
        executable = str(getattr(sys, "executable", "") or "")
        cwd = os.getcwd()
        with open(path, "a", encoding="utf-8") as f:
            f.write(
                f"[{stamp}] {message}\n"
                f"  frozen={frozen}\n"
                f"  executable={executable}\n"
                f"  argv={sys.argv!r}\n"
                f"  cwd={cwd}\n"
            )
        return path
    except Exception:
        return ""


def log_external_error(context, exc=None, details=""):
    """Persist browser/magnet launch errors even in a --noconsole build."""
    import traceback

    try:
        path = external_error_log_path()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "",
            "=" * 78,
            f"[{stamp}] {context}",
            f"argv={sys.argv!r}",
        ]
        if details:
            lines.append(str(details))
        if exc is not None:
            lines.append(f"{type(exc).__name__}: {exc}")
            tb = traceback.format_exc()
            if tb and tb.strip() != "NoneType: None":
                lines.append(tb.rstrip())

        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path
    except Exception:
        return ""


def normalize_external_source(raw):
    """Normalize a browser/file-association argument without contacting network."""
    source = str(raw or "").strip()
    if not source:
        return ""

    # Windows/browser protocol handlers occasionally preserve quotes or angle
    # brackets around a URI. Remove only wrappers, never characters inside it.
    source = source.strip().strip("\x00")
    while (
        len(source) >= 2
        and (
            (source[0] == '"' and source[-1] == '"')
            or (source[0] == "'" and source[-1] == "'")
            or (source[0] == "<" and source[-1] == ">")
        )
    ):
        source = source[1:-1].strip()

    # Some launchers hand an URL-encoded protocol argument to the registered
    # application. Decode only when the beginning clearly looks encoded.
    lower = source.lower()
    if lower.startswith("magnet%3a") or lower.startswith("file%3a"):
        try:
            source = unquote(source)
        except Exception:
            pass

    # Be tolerant of a literal "URL:" prefix from older protocol launchers.
    if source.lower().startswith("url:magnet:"):
        source = source[4:].strip()

    if source.lower().startswith("magnet:"):
        # Normalize scheme casing only.
        source = "magnet:" + source[len("magnet:"):]
        try:
            parsed = urlparse(source)
            params = parse_qs(parsed.query, keep_blank_values=True)
            xt_values = [
                str(v or "").strip().lower()
                for v in params.get("xt", [])
            ]
            if not any(
                v.startswith("urn:btih:") or v.startswith("urn:btmh:")
                for v in xt_values
            ):
                raise ValueError(
                    "Magnet link does not contain a supported BitTorrent "
                    "info-hash (xt=urn:btih: or xt=urn:btmh:)."
                )
        except Exception as e:
            raise ValueError(f"Invalid magnet link: {e}") from e
        return source

    # Browser/file shell launch may provide file:///C:/... form.
    if source.lower().startswith("file:"):
        try:
            parsed = urlparse(source)
            path = unquote(parsed.path or "")
            if sys.platform == "win32" and re.match(r"^/[A-Za-z]:/", path):
                path = path[1:]
            source = path.replace("/", os.sep)
        except Exception:
            pass

    if source.lower().endswith(".torrent"):
        source = os.path.abspath(os.path.expanduser(source))
        return source

    return ""


def forward_source_to_running_instance(source):
    """Forward a source to the existing GUI instance with bounded retries."""
    source = normalize_external_source(source)
    if not source:
        raise ValueError("No valid magnet or .torrent source was supplied.")

    last_error = None
    for attempt in range(1, 4):
        try:
            folder = ipc_folder()
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(
                folder,
                f"{time.time_ns()}-{os.getpid()}-{attempt}.msg",
            )
            temp = path + ".tmp"
            with open(temp, "w", encoding="utf-8", newline="") as f:
                f.write(source)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            os.replace(temp, path)
            return True
        except Exception as e:
            last_error = e
            time.sleep(0.12 * attempt)

    raise RuntimeError(
        f"Could not forward the link to the running app: {last_error}"
    )


def command_line_source():
    """Return the first valid browser/.torrent source from command-line args."""
    args = [str(a) for a in sys.argv[1:] if str(a).strip()]
    if not args:
        return ""

    # Do not assume the source is argv[1]; protocol launchers can add switches.
    errors = []
    for raw in args:
        try:
            source = normalize_external_source(raw)
            if source:
                return source
        except Exception as e:
            errors.append(f"{raw!r}: {type(e).__name__}: {e}")

    if errors:
        log_external_error(
            "Command-line source was present but invalid.",
            details="\n".join(errors),
        )
    return ""



# ---------------------------------------------------------------------------
# Proxy manager
# ---------------------------------------------------------------------------
BUY_ME_A_COFFEE_URL = "https://buymeacoffee.com/davekaiser"

class ProxyManager:
    def __init__(self):
        self.proxies = []
        self.index = -1
        self.status = {}
        self.detected_type = {}
        self.lock = threading.Lock()

    def load(self, text):
        parsed, errors = [], []
        for line_no, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                errors.append(f"Line {line_no}: missing ':' -> {line}")
                continue
            ip, _, port = line.rpartition(":")
            ip, port = ip.strip(), port.strip()
            if not ip or not port.isdigit():
                errors.append(f"Line {line_no}: invalid entry -> {line}")
                continue
            parsed.append((ip, int(port)))
        with self.lock:
            self.proxies = parsed
            self.index = 0 if parsed else -1
            self.status = {f"{ip}:{port}": "untested" for ip, port in parsed}
            self.detected_type = {}
        return parsed, errors

    def current(self):
        with self.lock:
            if 0 <= self.index < len(self.proxies):
                return self.proxies[self.index]
            return None

    def current_key(self):
        cur = self.current()
        return f"{cur[0]}:{cur[1]}" if cur else None

    def advance(self):
        with self.lock:
            if not self.proxies:
                return None
            self.index += 1
            if self.index >= len(self.proxies):
                self.index = -1
                return None
            return self.proxies[self.index]

    def select_proxy(self, proxy):
        with self.lock:
            try:
                self.index = self.proxies.index(proxy)
                return True
            except ValueError:
                return False

    def set_status(self, key, status):
        with self.lock:
            if key is not None:
                self.status[key] = status

    def set_detected_type(self, key, proxy_type):
        with self.lock:
            if key is not None and proxy_type:
                self.detected_type[key] = proxy_type

    def get_detected_type(self, key):
        with self.lock:
            return self.detected_type.get(key, "")

    def snapshot(self):
        with self.lock:
            return [
                (
                    f"{ip}:{port}",
                    self.detected_type.get(f"{ip}:{port}", ""),
                    self.status.get(f"{ip}:{port}", "untested"),
                    i == self.index,
                )
                for i, (ip, port) in enumerate(self.proxies)
            ]

    def has_any(self):
        with self.lock:
            return len(self.proxies) > 0


# ---------------------------------------------------------------------------
# One torrent's live state (shared between engine thread and GUI)
# ---------------------------------------------------------------------------
class TorrentItem:
    _ids = itertools.count(1)

    def __init__(self, source, save_path, torrent_dir):
        self.uid = next(self._ids)
        self.source = source
        self.save_path = save_path
        self.torrent_dir = torrent_dir
        self.torrent_file_saved = False
        self.torrent_file_path = ""
        self.handle = None
        self.name = initial_display_name(source)
        self.progress = 0.0

        # UI download speed means actual FILE PAYLOAD only.
        # network_rate includes BitTorrent protocol/connection chatter.
        self.dl_rate = 0.0
        self.network_rate = 0.0
        self.payload_total = 0
        self.state = "queued"
        self.total = 0
        self.downloaded = 0
        self.peers = 0
        self.files = []
        self.piece_map = []
        self.piece_len = 0
        self.error = ""
        self.identity = torrent_identity_from_source(source)

        # Runtime-only magnet metadata discovery state. These counters are
        # intentionally reset by an explicit Retry; Failed queue state itself
        # is persisted so it will never auto-retry after application restart.
        self.metadata_attempt = 0
        self.metadata_attempt_started = 0.0
        self.metadata_tracker_errors = 0
        self.metadata_tracker_replies = 0
        self.metadata_bootstrap_started = 0.0
        self.metadata_last_dht_kick = 0.0
        self.metadata_dht_wait_logged = False

        self.peer_attempt = 0
        self.peer_attempt_started = 0.0
        self.peer_tracker_errors = 0
        self.peer_tracker_replies = 0
        self.peer_checking_since = 0.0
        self.peer_attempt_payload_start = 0


# ---------------------------------------------------------------------------
# Download engine: one libtorrent session, many torrents
# ---------------------------------------------------------------------------
def safe_libtorrent_startup_settings():
    """Start libtorrent without the Windows-sensitive default network race."""
    return {
        "enable_dht": False,
        "enable_lsd": False,
        "enable_upnp": False,
        "enable_natpmp": False,
        "enable_incoming_tcp": False,
        "enable_incoming_utp": False,
        "listen_interfaces": "0.0.0.0:0",
    }


def compatible_libtorrent_settings(max_active_downloads=5):
    """Minimal conservative settings for supported Windows libtorrent wheels."""
    max_active_downloads = max(1, int(max_active_downloads or 5))
    return {
        "enable_dht": True,
        "enable_lsd": False,
        "enable_upnp": False,
        "enable_natpmp": False,
        "unchoke_slots_limit": 0,
        "allowed_fast_set_size": 0,
        "num_optimistic_unchoke_slots": 0,
        "active_downloads": max_active_downloads,
        "active_limit": max(500, max_active_downloads),
        "dont_count_slow_torrents": False,
        "auto_manage_interval": 1,
    }


class DownloadEngine:
    PROXY_TEST_TIMEOUT = 1.0
    TORRENT_PROXY_TEST_TIMEOUT = 6.0
    PROXY_HEALTH_INTERVAL = 4.0
    METADATA_PROXY_TIMEOUT = 8.0

    # Magnet metadata is not allowed to search forever. One "attempt" is a
    # bounded discovery window in which libtorrent may use the configured
    # trackers/DHT/peer discovery. Three misses means the torrent becomes
    # Failed and stays offline until the user explicitly chooses Retry.
    METADATA_MAX_ATTEMPTS = 3
    METADATA_ATTEMPT_SECONDS = 15.0

    # Direct-mode magnet attempts only become meaningful once DHT has actually
    # bootstrapped. "enable_dht=True" alone does not prove there are live DHT
    # nodes available to perform a lookup.
    DHT_BOOTSTRAP_GRACE_SECONDS = 20.0
    DHT_BOOTSTRAP_RETRY_SECONDS = 5.0
    DHT_BOOTSTRAP_NODES = (
        ("dht.libtorrent.org", 25401),
        ("router.bittorrent.com", 6881),
        ("dht.transmissionbt.com", 6881),
        ("router.bt.ouinet.work", 6881),
    )

    # Metadata may be available while the swarm itself has no usable peers.
    # Do not let that state retry forever either.
    PEER_MAX_ATTEMPTS = 3
    PEER_ATTEMPT_SECONDS = 15.0

    # A discovery attempt does not have to waste the entire timeout when the
    # network has already produced strong negative evidence. Five tracker
    # outcomes with no usable peer/metadata after 5 seconds may end the current
    # attempt early. There are still three complete attempts before Failed.
    DISCOVERY_EVIDENCE_COUNT = 5
    DISCOVERY_MIN_SECONDS = 5.0

    def __init__(self, proxy_mgr, event_q, max_active_downloads=5):
        self.proxy_mgr = proxy_mgr
        self.event_q = event_q
        self.session = None
        self.items = {}
        self.lock = threading.Lock()
        # Re-entrant because an explicit user Reload/Recheck removes an
        # existing swarm and then goes through the normal add_torrent path.
        self._add_lock = threading.RLock()
        self.max_active_downloads = max(
            1, min(100, int(max_active_downloads or 5))
        )
        self.thread = None
        self.stop_flag = threading.Event()
        self.use_proxy = False
        self.proxy_mode = "Auto"
        self._last_progress_time = {}
        self._last_proxy_health_check = 0.0
        self._last_diag_time = {}
        self._last_diag_signature = {}
        self._last_alert_time = {}
        self._running = False

    def _emit(self, kind, payload=None):
        self.event_q.put((kind, payload))

    def set_max_active_downloads(self, value):
        self.max_active_downloads = max(1, min(100, int(value or 5)))
        if self.session is not None:
            try:
                self.session.apply_settings({
                    "active_downloads": self.max_active_downloads,
                    "active_limit": max(500, self.max_active_downloads),
                    "dont_count_slow_torrents": False,
                    "auto_manage_interval": 1,
                })
            except Exception as e:
                self._emit(
                    "status_detail",
                    {
                        "level": "ERROR",
                        "message": f"Could not apply queue limit: {e}",
                    },
                )

    def _existing_identity_item(self, identity):
        if not identity:
            return None
        with self.lock:
            for item in self.items.values():
                if getattr(item, "identity", "") == identity:
                    return item
        return None

    def _make_session(self):
        startup_settings = safe_libtorrent_startup_settings()

        log_startup_diagnostic(
            "libtorrent session: constructing with safe startup settings"
        )
        session = lt.session(startup_settings)
        log_startup_diagnostic(
            "libtorrent session: safe startup constructor returned"
        )

        log_startup_diagnostic(
            "libtorrent session: enabling download runtime settings"
        )
        session.apply_settings(
            compatible_libtorrent_settings(self.max_active_downloads)
        )
        log_startup_diagnostic(
            "libtorrent session: download runtime settings applied"
        )

        try:
            cat = lt.alert.category_t
            mask = (
                cat.error_notification
                | cat.tracker_notification
                | cat.status_notification
            )
            session.set_alert_mask(mask)
            log_startup_diagnostic(
                "libtorrent session: alert mask configured"
            )
        except Exception as e:
            log_external_error(
                "Could not configure libtorrent alert mask.",
                exc=e,
            )

        return session

    def _dht_node_count(self):
        """Return live DHT routing-table nodes, or -1 if binding lacks it."""
        if self.session is None:
            return -1
        try:
            status = self.session.status()
            return int(getattr(status, "dht_nodes", -1))
        except Exception:
            return -1

    def _kick_dht_bootstrap(self):
        """Add libtorrent-documented DHT bootstrap/router nodes."""
        if self.session is None:
            return
        try:
            if not bool(
                self.session.get_settings().get("enable_dht", False)
            ):
                return
        except Exception:
            return

        for host, port in self.DHT_BOOTSTRAP_NODES:
            try:
                self.session.add_dht_node((host, int(port)))
            except Exception:
                pass

    def _item_for_handle(self, handle):
        if handle is None:
            return None
        with self.lock:
            items = list(self.items.values())
        for item in items:
            if item.handle is None:
                continue
            try:
                if item.handle == handle:
                    return item
            except Exception:
                continue
        return None

    def _attempt_should_end(self, elapsed, errors, replies, peers=0, rate=0.0):
        """Return True when a discovery round has enough evidence to end.

        One tracker failure is not enough to call a torrent dead because DHT,
        PEX, or another tracker may still find peers. But once the minimum
        observation period has elapsed, several tracker outcomes plus zero
        usable peers/activity are enough to mark THIS attempt unsuccessful.
        """
        if elapsed >= self.PEER_ATTEMPT_SECONDS:
            return True

        outcomes = int(errors or 0) + int(replies or 0)
        return (
            elapsed >= self.DISCOVERY_MIN_SECONDS
            and outcomes >= self.DISCOVERY_EVIDENCE_COUNT
            and int(peers or 0) <= 0
            and float(rate or 0.0) <= 0.0
        )

    def _force_metadata_discovery(self, item):
        """Ask libtorrent to refresh discovery for the next bounded attempt."""
        h = item.handle
        if h is None:
            return
        try:
            h.force_reannounce()
        except Exception:
            pass
        try:
            if hasattr(h, "force_dht_announce"):
                h.force_dht_announce()
        except Exception:
            pass

    def _fail_metadata_torrent(self, item):
        """Stop all networking for a magnet that missed all metadata tries."""
        tracker_failures = int(
            getattr(item, "metadata_tracker_errors", 0) or 0
        )
        tracker_replies = int(
            getattr(item, "metadata_tracker_replies", 0) or 0
        )
        peer_count = int(getattr(item, "peers", 0) or 0)
        dht_nodes = self._dht_node_count()

        item.state = "error"
        item.error = (
            f"Metadata was not obtained after "
            f"{self.METADATA_MAX_ATTEMPTS} full discovery attempts "
            f"({int(self.METADATA_ATTEMPT_SECONDS)}s each). "
            f"Last attempt: peers={peer_count}, DHT nodes={dht_nodes}, "
            f"tracker replies={tracker_replies}, "
            f"tracker failures={tracker_failures}. "
            f"This does not prove the swarm is dead; manual Retry is available."
        )
        item.dl_rate = 0.0
        item.network_rate = 0.0
        item.peers = 0

        h = item.handle
        if h is not None:
            try:
                h.pause()
            except Exception:
                pass
            if self.session is not None:
                try:
                    # No delete_files flag: payload/partial files remain.
                    self.session.remove_torrent(h)
                except Exception:
                    pass
        item.handle = None

        self._last_progress_time.pop(item.uid, None)
        self._last_diag_time.pop(item.uid, None)
        self._last_diag_signature.pop(item.uid, None)

        self._emit(
            "status_detail",
            {
                "level": "ERROR",
                "message": (
                    f"{item.name}: metadata attempt "
                    f"{self.METADATA_MAX_ATTEMPTS}/"
                    f"{self.METADATA_MAX_ATTEMPTS} failed after full discovery "
                    f"windows. Metadata was not obtained, so the torrent was "
                    f"stopped and kept in the list as Failed. Right-click "
                    f"Retry to try discovery again."
                ),
            },
        )
        self._emit("queue_changed")
        self._emit("torrents")

    def _metadata_discovery_tick(self, item, status):
        """Run three meaningful metadata discovery attempts.

        For direct magnets, don't burn attempts while DHT reports zero live
        routing-table nodes and trackers have returned nothing.
        """
        if bool(getattr(status, "has_metadata", False)):
            if item.metadata_attempt:
                self._emit(
                    "status_detail",
                    {
                        "level": "OK",
                        "message": (
                            f"{item.name}: metadata received on attempt "
                            f"{item.metadata_attempt}/"
                            f"{self.METADATA_MAX_ATTEMPTS}."
                        ),
                    },
                )
            item.metadata_attempt = 0
            item.metadata_attempt_started = 0.0
            item.metadata_tracker_errors = 0
            item.metadata_tracker_replies = 0
            item.metadata_bootstrap_started = 0.0
            item.metadata_last_dht_kick = 0.0
            item.metadata_dht_wait_logged = False
            return True

        now = time.time()
        peers = int(getattr(status, "num_peers", 0) or 0)
        tracker_errors = int(item.metadata_tracker_errors or 0)
        tracker_replies = int(item.metadata_tracker_replies or 0)

        try:
            dht_enabled = bool(
                self.session.get_settings().get("enable_dht", False)
            )
        except Exception:
            dht_enabled = False

        dht_nodes = self._dht_node_count() if dht_enabled else -1

        waiting_for_dht = (
            dht_enabled
            and dht_nodes == 0
            and tracker_replies <= 0
            and peers <= 0
        )

        # If libtorrent exposes the DHT node count and it is still exactly 0,
        # bootstrap first. This is a network-discovery problem, not yet a
        # torrent attempt.
        if waiting_for_dht and item.metadata_attempt <= 0:
            if item.metadata_bootstrap_started <= 0:
                item.metadata_bootstrap_started = now

            if (
                item.metadata_last_dht_kick <= 0
                or now - item.metadata_last_dht_kick
                >= self.DHT_BOOTSTRAP_RETRY_SECONDS
            ):
                item.metadata_last_dht_kick = now
                self._kick_dht_bootstrap()

            if not item.metadata_dht_wait_logged:
                item.metadata_dht_wait_logged = True
                self._emit(
                    "status_detail",
                    {
                        "level": "TORRENT",
                        "message": (
                            f"{item.name}: DHT is enabled but currently has "
                            f"0 live nodes. Warming up DHT before counting "
                            f"metadata attempts. Tracker failures alone do not "
                            f"count as an attempt."
                        ),
                    },
                )

            bootstrap_elapsed = now - item.metadata_bootstrap_started
            if bootstrap_elapsed < self.DHT_BOOTSTRAP_GRACE_SECONDS:
                return True

            item.state = "error"
            item.error = (
                f"Torrent discovery unavailable: DHT remained at 0 live nodes "
                f"for {int(self.DHT_BOOTSTRAP_GRACE_SECONDS)} seconds and no "
                f"tracker returned peers. Tracker failures={tracker_errors}. "
                f"This indicates a DHT/UDP/network-discovery problem, not proof "
                f"that the swarm has no seeders. Manual Retry is available."
            )

            h = item.handle
            if h is not None:
                try:
                    h.pause()
                except Exception:
                    pass
                if self.session is not None:
                    try:
                        self.session.remove_torrent(h)
                    except Exception:
                        pass

            item.handle = None
            item.dl_rate = 0.0
            item.network_rate = 0.0
            item.peers = 0
            self._last_progress_time.pop(item.uid, None)
            self._last_diag_time.pop(item.uid, None)
            self._last_diag_signature.pop(item.uid, None)

            self._emit(
                "status_detail",
                {
                    "level": "ERROR",
                    "message": (
                        f"{item.name}: DHT bootstrap failed (0 live nodes) and "
                        f"trackers returned no peers. Torrent kept as Failed. "
                        f"This is a discovery/network failure, not a conclusion "
                        f"that the torrent has no seeders."
                    ),
                },
            )
            self._emit("queue_changed")
            self._emit("torrents")
            return False

        if item.metadata_attempt <= 0:
            item.metadata_attempt = 1
            item.metadata_attempt_started = now
            item.metadata_tracker_errors = 0
            item.metadata_tracker_replies = 0
            item.metadata_dht_wait_logged = False
            self._force_metadata_discovery(item)

            dht_text = (
                f"{dht_nodes} live DHT node(s)"
                if dht_nodes >= 0
                else "DHT node count unavailable"
            )
            self._emit(
                "status_detail",
                {
                    "level": "TORRENT",
                    "message": (
                        f"{item.name}: metadata attempt 1/"
                        f"{self.METADATA_MAX_ATTEMPTS} started — "
                        f"{dht_text}; looking for a metadata peer."
                    ),
                },
            )
            return True

        elapsed = now - item.metadata_attempt_started
        tracker_errors = int(item.metadata_tracker_errors or 0)
        tracker_replies = int(item.metadata_tracker_replies or 0)
        dht_nodes = self._dht_node_count() if dht_enabled else -1

        if elapsed < self.METADATA_ATTEMPT_SECONDS:
            return True

        if item.metadata_attempt >= self.METADATA_MAX_ATTEMPTS:
            self._fail_metadata_torrent(item)
            return False

        self._emit(
            "status_detail",
            {
                "level": "TORRENT",
                "message": (
                    f"{item.name}: metadata attempt {item.metadata_attempt}/"
                    f"{self.METADATA_MAX_ATTEMPTS} failed after the full "
                    f"{int(self.METADATA_ATTEMPT_SECONDS)}s window — "
                    f"metadata=no, peers={peers}, DHT nodes={dht_nodes}, "
                    f"tracker replies={tracker_replies}, "
                    f"tracker failures={tracker_errors}."
                ),
            },
        )

        item.metadata_attempt += 1
        item.metadata_attempt_started = now
        item.metadata_tracker_errors = 0
        item.metadata_tracker_replies = 0

        self._force_metadata_discovery(item)
        self._kick_dht_bootstrap()

        self._emit(
            "status_detail",
            {
                "level": "TORRENT",
                "message": (
                    f"{item.name}: metadata attempt {item.metadata_attempt}/"
                    f"{self.METADATA_MAX_ATTEMPTS} started — "
                    f"DHT nodes={self._dht_node_count()}."
                ),
            },
        )
        return True

    def _fail_no_peers_torrent(self, item):
        """Stop networking after three complete no-peer discovery attempts."""
        tracker_failures = int(
            getattr(item, "peer_tracker_errors", 0) or 0
        )
        tracker_replies = int(
            getattr(item, "peer_tracker_replies", 0) or 0
        )

        item.state = "error"
        current_peers = int(getattr(item, "peers", 0) or 0)
        network_rate = float(getattr(item, "network_rate", 0.0) or 0.0)

        item.error = (
            f"File-data discovery failed after {self.PEER_MAX_ATTEMPTS} "
            f"attempts; connected peers never delivered usable payload. "
            f"Last attempt: connected peers={current_peers}, "
            f"payload rate=0 B/s, protocol/network="
            f"{human_rate(network_rate)}, tracker replies={tracker_replies}, "
            f"tracker failures={tracker_failures}. Manual Retry is available."
        )
        item.dl_rate = 0.0
        item.network_rate = 0.0
        item.peers = 0

        h = item.handle
        if h is not None:
            try:
                h.pause()
            except Exception:
                pass
            if self.session is not None:
                try:
                    # Remove live swarm only. Never delete payload/partial data.
                    self.session.remove_torrent(h)
                except Exception:
                    pass

        item.handle = None
        self._last_progress_time.pop(item.uid, None)
        self._last_diag_time.pop(item.uid, None)
        self._last_diag_signature.pop(item.uid, None)

        self._emit(
            "status_detail",
            {
                "level": "ERROR",
                "message": (
                    f"{item.name}: data attempt "
                    f"{self.PEER_MAX_ATTEMPTS}/{self.PEER_MAX_ATTEMPTS} "
                    f"failed — no actual file payload was received. Torrent "
                    f"stopped and kept in the list as Failed. Right-click "
                    f"Retry to try again later."
                ),
            },
        )
        self._emit("queue_changed")
        self._emit("torrents")

    def _peer_discovery_tick(self, item, status):
        """Bound the metadata-ready / zero-peer state to three attempts."""
        if not bool(getattr(status, "has_metadata", False)):
            return True

        # Do not count a short local file/resume-data check as peer failure,
        # but never let a stale "checking" state disable peer discovery forever.
        state_text = str(getattr(status, "state", "")).lower()
        now = time.time()
        if "checking" in state_text:
            if item.peer_checking_since <= 0:
                item.peer_checking_since = now
            if now - item.peer_checking_since < 8.0:
                return True
        else:
            item.peer_checking_since = 0.0

        peers = int(getattr(status, "num_peers", 0) or 0)
        network_rate = float(
            getattr(status, "download_rate", 0.0) or 0.0
        )
        payload_rate = float(
            getattr(status, "download_payload_rate", 0.0) or 0.0
        )
        payload_total = int(
            getattr(status, "total_payload_download", 0) or 0
        )

        # A completed connection/handshake is NOT a usable download source.
        # Success requires actual file payload bytes.
        payload_received_this_attempt = (
            payload_total > int(item.peer_attempt_payload_start or 0)
        )
        if payload_rate > 0 or payload_received_this_attempt:
            if item.peer_attempt:
                self._emit(
                    "status_detail",
                    {
                        "level": "OK",
                        "message": (
                            f"{item.name}: actual file payload received on "
                            f"attempt {item.peer_attempt}/"
                            f"{self.PEER_MAX_ATTEMPTS} — "
                            f"payload={human_rate(payload_rate)}, "
                            f"connected peers={peers}."
                        ),
                    },
                )
            item.peer_attempt = 0
            item.peer_attempt_started = 0.0
            item.peer_tracker_errors = 0
            item.peer_tracker_replies = 0
            item.peer_attempt_payload_start = payload_total
            return True

        # `now` was captured above so checking-state grace and discovery
        # attempts use the same clock sample.
        if item.peer_attempt <= 0:
            item.peer_attempt = 1
            item.peer_attempt_started = now
            item.peer_tracker_errors = 0
            item.peer_tracker_replies = 0
            item.peer_attempt_payload_start = payload_total
            self._emit(
                "status_detail",
                {
                    "level": "TORRENT",
                    "message": (
                        f"{item.name}: data attempt 1/"
                        f"{self.PEER_MAX_ATTEMPTS} started — metadata is ready; "
                        f"waiting for actual file payload, not protocol chatter."
                    ),
                },
            )
            return True

        elapsed = now - item.peer_attempt_started
        tracker_errors = int(item.peer_tracker_errors or 0)
        tracker_replies = int(item.peer_tracker_replies or 0)

        if not self._attempt_should_end(
            elapsed,
            tracker_errors,
            tracker_replies,
            peers=peers,
            rate=payload_rate,
        ):
            return True

        evidence_failed = (
            elapsed < self.PEER_ATTEMPT_SECONDS
            and elapsed >= self.DISCOVERY_MIN_SECONDS
            and (tracker_errors + tracker_replies)
            >= self.DISCOVERY_EVIDENCE_COUNT
        )

        if item.peer_attempt >= self.PEER_MAX_ATTEMPTS:
            self._fail_no_peers_torrent(item)
            return False

        self._emit(
            "status_detail",
            {
                "level": "TORRENT",
                "message": (
                    f"{item.name}: data attempt {item.peer_attempt}/"
                    f"{self.PEER_MAX_ATTEMPTS} failed — connected peers="
                    f"{peers}, payload=0 B/s, protocol/network="
                    f"{human_rate(network_rate)}, "
                    f"tracker replies={tracker_replies}, "
                    f"tracker failures={tracker_errors}"
                    + (
                        " (ended early: enough negative discovery evidence)."
                        if evidence_failed
                        else "."
                    )
                ),
            },
        )

        item.peer_attempt += 1
        item.peer_attempt_started = now
        item.peer_tracker_errors = 0
        item.peer_tracker_replies = 0
        item.peer_attempt_payload_start = payload_total

        self._force_metadata_discovery(item)

        self._emit(
            "status_detail",
            {
                "level": "TORRENT",
                "message": (
                    f"{item.name}: data attempt {item.peer_attempt}/"
                    f"{self.PEER_MAX_ATTEMPTS} started — waiting for actual "
                    f"file payload."
                ),
            },
        )
        return True

    def _drain_alerts(self):
        if self.session is None:
            return
        try:
            alerts = self.session.pop_alerts()
        except Exception:
            return

        important = (
            "tracker_error",
            "tracker_warning",
            "tracker_reply",
            "metadata_received",
            "torrent_error",
            "file_error",
            "listen_failed",
            "peer_error",
            "dht_error",
            "url_seed",
            "fastresume_rejected",
        )

        now = time.time()
        for alert in alerts:
            try:
                what = alert.what() if hasattr(alert, "what") else type(alert).__name__
                message = alert.message() if hasattr(alert, "message") else str(alert)
            except Exception:
                continue

            lower = str(what).lower()
            if not any(key in lower for key in important):
                continue

            # A magnet can contain many stale tracker URLs. During bounded
            # metadata discovery, count those failures/replies and summarize
            # them at the end of each attempt instead of flooding App Status
            # with one line per tracker retry.
            if (
                "tracker_error" in lower
                or "tracker_warning" in lower
                or "tracker_reply" in lower
            ):
                try:
                    alert_handle = getattr(alert, "handle", None)
                except Exception:
                    alert_handle = None
                item = self._item_for_handle(alert_handle)
                if item is not None:
                    if item.state == "metadata":
                        if "tracker_reply" in lower:
                            item.metadata_tracker_replies += 1
                        else:
                            item.metadata_tracker_errors += 1
                        continue

                    if (
                        item.state == "downloading"
                        and item.peer_attempt > 0
                        and item.dl_rate <= 0
                    ):
                        if "tracker_reply" in lower:
                            item.peer_tracker_replies += 1
                        else:
                            item.peer_tracker_errors += 1
                        continue

                # Routine tracker errors/replies are discovery evidence, not
                # useful line-by-line App Status events.
                if (
                    "tracker_error" in lower
                    or "tracker_warning" in lower
                    or "tracker_reply" in lower
                ):
                    continue

            signature = f"{what}|{message}"
            last = self._last_alert_time.get(signature, 0.0)
            if now - last < 4.0:
                continue
            self._last_alert_time[signature] = now
            self._emit(
                "engine_alert",
                {"what": str(what), "message": str(message)},
            )

    def _torrent_diagnostic(self, item, status):
        now = time.time()
        last = self._last_diag_time.get(item.uid, 0.0)

        try:
            tracker = str(getattr(status, "current_tracker", "") or "")
        except Exception:
            tracker = ""

        if not tracker:
            try:
                trackers = item.handle.trackers()
                if trackers:
                    first = trackers[0]
                    tracker = (
                        first.get("url", "")
                        if isinstance(first, dict)
                        else str(getattr(first, "url", "") or "")
                    )
            except Exception:
                tracker = ""

        try:
            seeds = int(getattr(status, "num_seeds", 0) or 0)
        except Exception:
            seeds = 0

        proxy = self.proxy_mgr.current_key() if self.use_proxy else "direct"
        proxy_type = ""
        if self.use_proxy and proxy:
            proxy_type = self.proxy_mgr.get_detected_type(proxy)

        try:
            dht_enabled = bool(self.session.get_settings().get("enable_dht", False))
        except Exception:
            dht_enabled = False
        dht_nodes = self._dht_node_count() if dht_enabled else -1

        payload = {
            "uid": item.uid,
            "name": item.name,
            "state": item.state,
            "metadata": bool(getattr(status, "has_metadata", False)),
            "progress": float(item.progress),
            "rate": float(item.dl_rate),
            "network_rate": float(getattr(item, "network_rate", 0.0)),
            "payload_total": int(getattr(item, "payload_total", 0)),
            "peers": int(item.peers),
            "seeds": seeds,
            "tracker": tracker,
            "proxy": proxy or "none",
            "proxy_type": proxy_type,
            "dht": dht_enabled,
            "dht_nodes": dht_nodes,
        }

        signature = (
            payload["state"],
            payload["metadata"],
            # Log peer presence changes, not every small count fluctuation.
            0 if payload["peers"] <= 0 else 1,
            0 if payload["seeds"] <= 0 else 1,
            # Log stopped-vs-moving rather than every rate fluctuation.
            0 if payload["rate"] <= 0 else 1,
            payload["proxy"],
            payload["proxy_type"],
            payload["dht"],
        )

        # App Status is an event log, not a heartbeat. The torrent row already
        # displays the current live state, so only report when something
        # materially changes instead of repeating the same status every 8s.
        if signature != self._last_diag_signature.get(item.uid):
            self._last_diag_signature[item.uid] = signature
            self._last_diag_time[item.uid] = now
            self._emit("torrent_diagnostic", payload)

    def _proxy_test_order(self, proxy):
        mode = (self.proxy_mode or "Auto").strip().upper()
        if mode == "HTTP":
            return ["HTTP"]
        if mode == "SOCKS5":
            return ["SOCKS5"]

        # Auto: try the most likely protocol first, then the other.
        _ip, port = proxy
        if port in (80, 81, 443, 8000, 8080, 8081, 8888, 3128):
            return ["HTTP", "SOCKS5"]
        return ["SOCKS5", "HTTP"]

    def _test_http_proxy(self, proxy, timeout=None):
        """Verify that an HTTP proxy can create a CONNECT tunnel.

        The app connects only to the proxy IP directly. example.com is reached
        through the proxy tunnel, never as a direct fallback.
        """
        if timeout is None:
            timeout = self.PROXY_TEST_TIMEOUT
        ip, port = proxy
        sock = None
        try:
            sock = socket.create_connection((ip, port), timeout=timeout)
            sock.settimeout(timeout)
            request = (
                "CONNECT example.com:443 HTTP/1.1\r\n"
                "Host: example.com:443\r\n"
                "Proxy-Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            sock.sendall(request)

            data = b""
            while b"\r\n" not in data and len(data) < 4096:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                data += chunk

            first_line = data.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            parts = first_line.split()
            if len(parts) >= 2 and parts[1] == "200":
                return True, "HTTP CONNECT accepted"
            return False, first_line or "No HTTP proxy response"
        except Exception as e:
            return False, str(e)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

    def _test_socks5_proxy(self, proxy, timeout=None):
        """Verify SOCKS5 no-auth negotiation and a proxied CONNECT request."""
        if timeout is None:
            timeout = self.PROXY_TEST_TIMEOUT
        ip, port = proxy
        sock = None
        try:
            sock = socket.create_connection((ip, port), timeout=timeout)
            sock.settimeout(timeout)

            # SOCKS5 greeting: version 5, one auth method, no authentication.
            sock.sendall(b"\x05\x01\x00")
            reply = sock.recv(2)
            if reply != b"\x05\x00":
                return False, "SOCKS5 no-auth negotiation rejected"

            host = b"example.com"
            request = (
                b"\x05\x01\x00\x03"
                + bytes([len(host)])
                + host
                + (443).to_bytes(2, "big")
            )
            sock.sendall(request)
            reply = sock.recv(4)
            if len(reply) < 2 or reply[0] != 5:
                return False, "Invalid SOCKS5 response"
            if reply[1] != 0:
                return False, f"SOCKS5 CONNECT failed (code {reply[1]})"
            return True, "SOCKS5 CONNECT accepted"
        except Exception as e:
            return False, str(e)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

    def _test_proxy(self, proxy):
        """Return (works, detected_type, detail)."""
        key = f"{proxy[0]}:{proxy[1]}"
        self.proxy_mgr.set_status(key, "testing")
        self._emit("proxy")
        self._emit(
            "proxy_connection",
            {
                "connected": False,
                "testing": True,
                "proxy": key,
                "type": "",
                "detail": "Testing proxy...",
            },
        )

        failures = []
        for kind in self._proxy_test_order(proxy):
            self._emit(
                "status_detail",
                {
                    "level": "PROXY",
                    "message": f"Testing {key} as {kind}...",
                },
            )
            if kind == "HTTP":
                ok, detail = self._test_http_proxy(proxy)
            else:
                ok, detail = self._test_socks5_proxy(proxy)

            if ok:
                self.proxy_mgr.set_detected_type(key, kind)
                self.proxy_mgr.set_status(key, "transport-ok")
                self._emit("proxy")

                visible_detail = detail
                if kind == "HTTP":
                    visible_detail += (
                        " — HTTP tunnel works, but BitTorrent peer ports have "
                        "NOT been verified yet."
                    )

                self._emit(
                    "proxy_connection",
                    {
                        "connected": False,
                        "testing": False,
                        "unverified": True,
                        "proxy": key,
                        "type": kind,
                        "detail": visible_detail,
                    },
                )
                self._emit(
                    "status_detail",
                    {
                        "level": "OK",
                        "message": (
                            f"Proxy {key} passed the {kind} TRANSPORT test only. "
                            "It is still TORRENT UNVERIFIED until an actual torrent "
                            "gets metadata/peer traffic through it."
                        ),
                    },
                )
                return True, kind, visible_detail
            failures.append(f"{kind}: {detail}")
            self._emit(
                "status_detail",
                {
                    "level": "PROXY",
                    "message": f"Proxy {key} failed {kind}: {detail}",
                },
            )

        self.proxy_mgr.set_status(key, "failed")
        self._emit("proxy")
        self._emit(
            "proxy_connection",
            {
                "connected": False,
                "testing": False,
                "proxy": key,
                "type": "",
                "detail": " | ".join(failures),
            },
        )
        return False, "", " | ".join(failures)

    def _quick_proxy_health(self, proxy):
        """Fast health probe for the currently active proxy.

        This verifies the proxy transport itself. It is intentionally separate
        from torrent progress so a slow/dead swarm is not automatically called
        a dead proxy.
        """
        if proxy is None:
            return False, "No active proxy."

        key = f"{proxy[0]}:{proxy[1]}"
        detected = self.proxy_mgr.get_detected_type(key)
        timeout = min(self.PROXY_TEST_TIMEOUT, 1.0)

        if detected == "HTTP":
            return self._test_http_proxy(proxy, timeout=timeout)
        if detected == "SOCKS5":
            return self._test_socks5_proxy(proxy, timeout=timeout)

        # Normally the active proxy has already been detected. If not, test
        # using the configured order and accept the first successful protocol.
        details = []
        for kind in self._proxy_test_order(proxy):
            if kind == "HTTP":
                ok, detail = self._test_http_proxy(proxy, timeout=timeout)
            else:
                ok, detail = self._test_socks5_proxy(proxy, timeout=timeout)
            if ok:
                self.proxy_mgr.set_detected_type(key, kind)
                return True, detail
            details.append(f"{kind}: {detail}")
        return False, " | ".join(details)

    def _make_torrent_probe_session(self, proxy, kind):
        """Create an isolated libtorrent session forced through one proxy.

        No torrent is added until the proxy settings have been applied. HTTP
        probes keep DHT disabled because HTTP CONNECT cannot safely proxy DHT
        UDP. SOCKS5 probes may use DHT through the SOCKS5 proxy.
        """
        base_settings = {
            "listen_interfaces": "0.0.0.0:0",
            "enable_incoming_tcp": False,
            "enable_incoming_utp": False,
            "enable_dht": False,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "upload_rate_limit": 0,
            "rate_limit_ip_overhead": False,
            "choking_algorithm": 0,
            "unchoke_slots_limit": 0,
            "allowed_fast_set_size": 0,
            "num_optimistic_unchoke_slots": 0,
        }

        session = lt.session()
        session.apply_settings(base_settings)

        settings = session.get_settings()
        settings["proxy_type"] = 4 if kind == "HTTP" else 2
        settings["proxy_hostname"] = proxy[0]
        settings["proxy_port"] = int(proxy[1])
        settings["proxy_peer_connections"] = True
        settings["proxy_tracker_connections"] = True
        settings["proxy_hostnames"] = True
        if "force_proxy" in settings:
            settings["force_proxy"] = True

        # HTTP is TCP-only. SOCKS5 may carry DHT UDP through UDP ASSOCIATE.
        settings["enable_dht"] = (kind == "SOCKS5")
        settings["enable_lsd"] = False
        settings["enable_upnp"] = False
        settings["enable_natpmp"] = False
        settings["enable_incoming_tcp"] = False
        settings["enable_incoming_utp"] = False
        session.apply_settings(settings)

        try:
            cat = lt.alert.category_t
            mask = (
                cat.error_notification
                | cat.tracker_notification
                | cat.peer_notification
                | cat.status_notification
            )
            session.set_alert_mask(mask)
        except Exception:
            pass

        return session

    def _probe_torrent_through_proxy(self, proxy, kind, source, stop_event=None):
        """Prove that this proxy can actually reach this torrent's swarm.

        Magnet pass: torrent metadata is received through the proxy.
        .torrent pass: at least one actual peer connection is established.
        """
        key = f"{proxy[0]}:{proxy[1]}"
        is_magnet = source.lower().startswith("magnet:")
        temp_dir = tempfile.mkdtemp(prefix="GetTorrent-proxy-test-")
        session = None
        handle = None
        alerts_seen = []

        try:
            session = self._make_torrent_probe_session(proxy, kind)

            if is_magnet:
                atp = lt.parse_magnet_uri(source)
                atp.save_path = temp_dir
                atp.flags &= ~lt.torrent_flags.seed_mode
                atp.flags &= ~lt.torrent_flags.upload_mode
            else:
                info = lt.torrent_info(source)
                atp = lt.add_torrent_params()
                atp.ti = info
                atp.save_path = temp_dir
                atp.flags &= ~lt.torrent_flags.seed_mode
                atp.flags &= ~lt.torrent_flags.upload_mode

                # Do not intentionally download payload during a proxy test.
                try:
                    atp.file_priorities = [0] * info.num_files()
                except Exception:
                    pass

            handle = session.add_torrent(atp)
            try:
                handle.set_upload_mode(False)
                handle.force_reannounce()
            except Exception:
                pass

            self._emit(
                "status_detail",
                {
                    "level": "PROXY",
                    "message": (
                        f"BITTORRENT TEST {key} ({kind}): connecting to the "
                        f"actual {'magnet swarm' if is_magnet else 'torrent swarm'}..."
                    ),
                },
            )

            started = time.time()
            last_status = None

            while time.time() - started < self.TORRENT_PROXY_TEST_TIMEOUT:
                if stop_event is not None and stop_event.is_set():
                    return False, "cancelled"

                try:
                    status = handle.status()
                    last_status = status

                    if is_magnet and status.has_metadata:
                        elapsed = time.time() - started
                        return (
                            True,
                            f"magnet metadata received through {key} in {elapsed:.1f}s",
                        )

                    if not is_magnet and status.num_peers > 0:
                        elapsed = time.time() - started
                        return (
                            True,
                            f"actual BitTorrent peer connected through {key} "
                            f"in {elapsed:.1f}s",
                        )

                    if float(
                        getattr(status, "download_payload_rate", 0.0)
                        or 0.0
                    ) > 0:
                        elapsed = time.time() - started
                        return (
                            True,
                            f"BitTorrent file payload observed through {key} "
                            f"in {elapsed:.1f}s",
                        )
                except Exception as e:
                    alerts_seen.append(f"status: {e}")

                try:
                    for alert in session.pop_alerts():
                        what = (
                            alert.what()
                            if hasattr(alert, "what")
                            else type(alert).__name__
                        )
                        message = (
                            alert.message()
                            if hasattr(alert, "message")
                            else str(alert)
                        )
                        lower = str(what).lower()

                        if any(
                            token in lower
                            for token in (
                                "tracker_error",
                                "tracker_warning",
                                "torrent_error",
                                "peer_error",
                                "dht_error",
                                "listen_failed",
                            )
                        ):
                            entry = f"{what}: {message}"
                            if entry not in alerts_seen:
                                alerts_seen.append(entry)

                        if "metadata_received" in lower and is_magnet:
                            elapsed = time.time() - started
                            return (
                                True,
                                f"metadata_received alert through {key} "
                                f"in {elapsed:.1f}s",
                            )
                except Exception as e:
                    alerts_seen.append(f"alerts: {e}")

                time.sleep(0.20)

            peers = 0
            seeds = 0
            tracker = ""
            metadata = False
            try:
                if last_status is not None:
                    peers = int(getattr(last_status, "num_peers", 0) or 0)
                    seeds = int(getattr(last_status, "num_seeds", 0) or 0)
                    tracker = str(
                        getattr(last_status, "current_tracker", "") or ""
                    )
                    metadata = bool(
                        getattr(last_status, "has_metadata", False)
                    )
            except Exception:
                pass

            reason = (
                f"no {'metadata' if is_magnet else 'peer connection'} after "
                f"{self.TORRENT_PROXY_TEST_TIMEOUT:.0f}s; "
                f"peers={peers}, seeds={seeds}, metadata={metadata}, "
                f"tracker={tracker or 'none'}"
            )
            if alerts_seen:
                reason += "; errors=" + " | ".join(alerts_seen[-4:])
            return False, reason

        except Exception as e:
            return False, f"BitTorrent probe exception: {type(e).__name__}: {e}"

        finally:
            if session is not None and handle is not None:
                try:
                    session.remove_torrent(handle)
                except Exception:
                    pass
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

    def _test_proxy_for_torrent(self, proxy, source, stop_event=None):
        """Test transport AND actual BitTorrent use for one torrent."""
        key = f"{proxy[0]}:{proxy[1]}"
        self.proxy_mgr.set_status(key, "torrent-testing")
        self._emit("proxy")

        failure_details = []

        for kind in self._proxy_test_order(proxy):
            if stop_event is not None and stop_event.is_set():
                return False, "", "cancelled"

            # First prove this protocol is actually spoken by the endpoint.
            if kind == "HTTP":
                transport_ok, transport_detail = self._test_http_proxy(proxy)
            else:
                transport_ok, transport_detail = self._test_socks5_proxy(proxy)

            if not transport_ok:
                detail = f"{kind} transport failed: {transport_detail}"
                failure_details.append(detail)
                self._emit(
                    "status_detail",
                    {
                        "level": "PROXY",
                        "message": f"{key}: {detail}",
                    },
                )
                continue

            self._emit(
                "status_detail",
                {
                    "level": "PROXY",
                    "message": (
                        f"{key}: {kind} tunnel works. Now verifying actual "
                        "BitTorrent tracker/peer traffic for this torrent..."
                    ),
                },
            )

            torrent_ok, torrent_detail = self._probe_torrent_through_proxy(
                proxy,
                kind,
                source,
                stop_event=stop_event,
            )

            if stop_event is not None and stop_event.is_set():
                return False, "", "cancelled"

            if torrent_ok:
                self.proxy_mgr.set_detected_type(key, kind)
                self.proxy_mgr.set_status(key, "torrent-ok")
                self._emit("proxy")
                self._emit(
                    "proxy_connection",
                    {
                        "connected": True,
                        "testing": False,
                        "proxy": key,
                        "type": kind,
                        "detail": (
                            f"BITTORRENT VERIFIED: {torrent_detail}. "
                            "Green means this proxy worked with the actual torrent."
                        ),
                        "torrent_verified": True,
                    },
                )
                self._emit(
                    "status_detail",
                    {
                        "level": "OK",
                        "message": (
                            f"BITTORRENT VERIFIED {key} ({kind}): "
                            f"{torrent_detail}"
                        ),
                    },
                )
                return True, kind, torrent_detail

            failure_details.append(f"{kind}: {torrent_detail}")
            self._emit(
                "status_detail",
                {
                    "level": "ERROR",
                    "message": (
                        f"BITTORRENT TEST FAILED {key} ({kind}): "
                        f"{torrent_detail}"
                    ),
                },
            )

        self.proxy_mgr.set_status(key, "torrent-blocked")
        self._emit("proxy")
        return False, "", " | ".join(failure_details)

    def _select_torrent_compatible_proxy(self, source):
        """Test all proxies in parallel and choose one proven for this torrent.

        Running probes in parallel keeps a six-proxy list near one probe timeout
        instead of six probe timeouts.
        """
        with self.proxy_mgr.lock:
            proxies = list(self.proxy_mgr.proxies)

        if not proxies:
            return None

        self._emit(
            "status_detail",
            {
                "level": "PROXY",
                "message": (
                    f"Running torrent-specific proxy tests against "
                    f"{len(proxies)} proxy address(es). A proxy is not accepted "
                    "until this torrent reaches metadata/peer traffic through it."
                ),
            },
        )

        results = queue.Queue()
        stop_event = threading.Event()

        def worker(index, proxy):
            ok, kind, detail = self._test_proxy_for_torrent(
                proxy,
                source,
                stop_event=stop_event,
            )
            if detail == "cancelled":
                return
            results.put((index, proxy, ok, kind, detail))

        for index, proxy in enumerate(proxies):
            threading.Thread(
                target=worker,
                args=(index, proxy),
                daemon=True,
            ).start()

        deadline = time.time() + self.TORRENT_PROXY_TEST_TIMEOUT + 3.0
        failures = {}
        received = 0

        while received < len(proxies) and time.time() < deadline:
            try:
                index, proxy, ok, kind, detail = results.get(timeout=0.20)
            except queue.Empty:
                continue

            received += 1
            if ok:
                stop_event.set()
                self.proxy_mgr.select_proxy(proxy)
                self.proxy_mgr.set_detected_type(
                    f"{proxy[0]}:{proxy[1]}",
                    kind,
                )
                self.proxy_mgr.set_status(
                    f"{proxy[0]}:{proxy[1]}",
                    "torrent-ok",
                )
                self._emit("proxy")
                self._emit(
                    "status_detail",
                    {
                        "level": "OK",
                        "message": (
                            f"Selected torrent-compatible proxy "
                            f"{proxy[0]}:{proxy[1]} ({kind})."
                        ),
                    },
                )
                return proxy

            failures[index] = detail

        stop_event.set()

        ordered_failures = [
            f"{proxies[i][0]}:{proxies[i][1]} -> {failures.get(i, 'no successful BitTorrent result')}"
            for i in range(len(proxies))
        ]
        self._emit(
            "proxy_connection",
            {
                "connected": False,
                "testing": False,
                "proxy": "",
                "type": "",
                "detail": "No proxy passed the torrent-specific test.",
                "torrent_verified": False,
            },
        )
        self._emit(
            "status_detail",
            {
                "level": "ERROR",
                "message": (
                    "NO TORRENT-COMPATIBLE PROXY FOUND. "
                    + " || ".join(ordered_failures)
                ),
            },
        )
        return None

    def _select_working_proxy(self):
        """Test current and subsequent proxies until one really works."""
        proxy = self.proxy_mgr.current()
        while proxy is not None:
            ok, kind, _detail = self._test_proxy(proxy)
            if ok:
                self._emit(
                    "status_detail",
                    {
                        "level": "PROXY",
                        "message": (
                            f"Using proxy {proxy[0]}:{proxy[1]} ({kind})."
                        ),
                    },
                )
                return proxy
            proxy = self.proxy_mgr.advance()

        self._emit(
            "proxy_connection",
            {
                "connected": False,
                "testing": False,
                "proxy": "",
                "type": "",
                "detail": "No working proxy remains in the list.",
            },
        )
        return None

    def _proxy_type_for(self, proxy):
        key = f"{proxy[0]}:{proxy[1]}"
        detected = self.proxy_mgr.get_detected_type(key)
        if detected == "HTTP":
            return 4
        if detected == "SOCKS5":
            return 2

        # This is only a fallback. Normal proxy-mode startup tests the proxy
        # first and records the protocol that actually worked.
        mode = (self.proxy_mode or "Auto").strip().upper()
        if mode == "HTTP":
            return 4
        if mode == "SOCKS5":
            return 2
        return 4 if proxy[1] in (80, 81, 443, 8000, 8080, 8081, 8888, 3128) else 2

    def _apply_proxy(self, proxy, reset_connections=False):
        """Apply direct/proxy networking.

        IMPORTANT: callers must never pass None while self.use_proxy is True.
        In proxy mode, peer payload, trackers and hostname lookups use the
        configured HTTP/SOCKS5 proxy. SOCKS5 can additionally proxy UDP/DHT.
        """
        if self.session is None:
            return False

        # Never silently fall back to direct networking while proxy mode is ON.
        if self.use_proxy and proxy is None:
            self._emit("error", "Proxy mode is ON but there is no active proxy. Network blocked.")
            return False

        s = self.session.get_settings()
        if proxy is None:
            s["proxy_type"] = 0
            s["proxy_hostname"] = ""
            s["proxy_port"] = 0
            s["proxy_peer_connections"] = False
            s["proxy_tracker_connections"] = False
            s["proxy_hostnames"] = False
            # Older libtorrent versions expose force_proxy; newer 2.x builds may not.
            if "force_proxy" in s:
                s["force_proxy"] = False
        else:
            ip, port = proxy
            s["proxy_type"] = self._proxy_type_for(proxy)
            s["proxy_hostname"] = ip
            s["proxy_port"] = port
            s["proxy_peer_connections"] = True
            s["proxy_tracker_connections"] = True
            s["proxy_hostnames"] = True
            if "force_proxy" in s:
                s["force_proxy"] = True

        # Download-only policy: allow protocol traffic, but never unchoke peers.
        s["upload_rate_limit"] = 0  # do not throttle protocol/control traffic
        s["rate_limit_ip_overhead"] = False
        s["choking_algorithm"] = 0
        s["unchoke_slots_limit"] = 0
        s["allowed_fast_set_size"] = 0
        s["num_optimistic_unchoke_slots"] = 0
        s["enable_incoming_tcp"] = False
        s["enable_incoming_utp"] = False
        # SOCKS5 can carry UDP via UDP ASSOCIATE. HTTP CONNECT is TCP-only,
        # so keep DHT disabled for HTTP proxies rather than allowing direct
        # UDP/DHT fallback.
        s["dht_bootstrap_nodes"] = ",".join(
            f"{host}:{port}"
            for host, port in self.DHT_BOOTSTRAP_NODES
        )
        s["use_dht_as_fallback"] = False
        if proxy is None:
            s["enable_dht"] = True
        else:
            s["enable_dht"] = (self._proxy_type_for(proxy) == 2)
        s["enable_lsd"] = False
        s["enable_upnp"] = False
        s["enable_natpmp"] = False
        s["active_downloads"] = self.max_active_downloads
        s["active_limit"] = max(500, self.max_active_downloads)
        s["dont_count_slow_torrents"] = False
        s["auto_manage_interval"] = 1
        self.session.apply_settings(s)

        if proxy is None:
            self._kick_dht_bootstrap()

        if reset_connections:
            # Drop any peers created under the previous network mode/proxy so
            # they cannot remain connected after a mode/proxy change.
            with self.lock:
                handle_states = [
                    (it.handle, it.state)
                    for it in self.items.values()
                    if it.handle is not None
                ]
            for h, _state in handle_states:
                try:
                    h.pause()
                except Exception:
                    pass
                try:
                    h.clear_peers()
                except Exception:
                    pass
            try:
                self.session.reopen_network_sockets()
            except Exception:
                pass
            for h, previous_state in handle_states:
                if previous_state in ("paused", "stopped", "finished", "queued"):
                    continue
                try:
                    h.resume()
                    h.set_upload_mode(False)
                    h.force_reannounce()
                except Exception:
                    pass
        return True

    def _sync_network_mode(self, reset_connections=False, source=None):
        """Enforce the UI-selected network mode on the current session."""
        if self.session is None:
            return True
        if self.use_proxy:
            if source:
                proxy = self._select_torrent_compatible_proxy(source)
            else:
                # No torrent source is available to prove BitTorrent
                # compatibility. Transport-only selection is allowed only as
                # an unverified temporary network configuration.
                proxy = self._select_working_proxy()
            if proxy is None:
                self._emit("error", "Proxy mode is ON but no suitable proxy is available.")
                with self.lock:
                    items = list(self.items.values())
                for item in items:
                    if item.handle is not None:
                        try:
                            item.handle.pause()
                        except Exception:
                            pass
                        item.state = "stopped"
                return False
            return self._apply_proxy(proxy, reset_connections=reset_connections)

        # Direct-only session: adding another magnet should not reconfigure
        # global libtorrent networking at all.
        return True

    def ensure_started(self, source=None):
        log_startup_diagnostic(
            f"engine.ensure_started entered; running={self._running}"
        )
        if self._running:
            # For a newly-added torrent, prove that the selected proxy works
            # with THAT torrent before changing/using the live session.
            return self._sync_network_mode(
                reset_connections=True,
                source=source,
            )
        if lt is None:
            self._emit("error", "libtorrent is not installed. Run: pip install libtorrent")
            return False
        try:
            log_startup_diagnostic(
                "engine.ensure_started: creating libtorrent session"
            )
            self.session = self._make_session()
            log_startup_diagnostic(
                "engine.ensure_started: libtorrent session created"
            )
        except Exception as e:
            self._emit("error", f"Failed to create session: {e}")
            return False

        if self.use_proxy:
            if not self.proxy_mgr.has_any() or self.proxy_mgr.current() is None:
                self._emit("error", "Proxy mode is ON but no proxy list is available.")
                return False

            if source:
                working_proxy = self._select_torrent_compatible_proxy(source)
                failure_text = (
                    "None of the proxies passed the torrent-specific test. "
                    "Downloading was not started."
                )
            else:
                working_proxy = self._select_working_proxy()
                failure_text = (
                    "No proxy passed even the transport test. "
                    "Downloading was not started."
                )

            if working_proxy is None:
                self._emit("error", failure_text)
                return False

            if not self._apply_proxy(working_proxy):
                return False
        else:
            # _make_session() already applied direct-mode socket/DHT settings.
            # Just seed DHT once; do not immediately re-apply global settings.
            self._kick_dht_bootstrap()

        self.stop_flag.clear()
        self._last_proxy_health_check = 0.0
        self._running = True
        self.thread = threading.Thread(
            target=self._run_guarded,
            daemon=True,
            name="TorrentDownloaderEngine",
        )
        self.thread.start()
        return True

    def _torrent_descriptor_path(self, item, torrent_name=None):
        """Choose a descriptor path without overwriting another swarm's file."""
        os.makedirs(item.torrent_dir, exist_ok=True)
        if (
            not item.source.lower().startswith("magnet:")
            and item.source.lower().endswith(".torrent")
        ):
            filename = os.path.basename(item.source)
        else:
            filename = safe_filename(torrent_name or item.name)
            if not filename.lower().endswith(".torrent"):
                filename += ".torrent"

        candidate = os.path.join(item.torrent_dir, filename)
        if not os.path.isfile(candidate):
            return candidate

        # Reusing the same descriptor for the same swarm is fine. The dangerous
        # case is two unrelated torrents resolving to the same filename.
        item_identity = (
            getattr(item, "identity", "")
            or torrent_identity_from_source(item.source)
        )
        existing_identity = torrent_identity_from_source(candidate)
        if (
            item_identity
            and existing_identity
            and item_identity == existing_identity
        ):
            return candidate

        if not item_identity:
            return candidate

        stem, ext = os.path.splitext(filename)
        if not ext:
            ext = ".torrent"
        suffix = hashlib.sha1(
            item_identity.encode("utf-8", "replace")
        ).hexdigest()[:10]
        unique_name = f"{stem} [{suffix}]{ext}"
        return os.path.join(item.torrent_dir, unique_name)

    def _save_torrent_descriptor(self, item):
        """Copy a local .torrent or save metadata acquired from a magnet."""
        if item.torrent_file_saved or not item.torrent_dir:
            return

        if not item.source.lower().startswith("magnet:") and os.path.isfile(item.source):
            try:
                dest = os.path.abspath(self._torrent_descriptor_path(item))
                src = os.path.abspath(item.source)
                if os.path.normcase(src) != os.path.normcase(dest):
                    shutil.copy2(src, dest)
                item.torrent_file_path = dest
                item.torrent_file_saved = True
                self._emit("status", f"Saved torrent file: {dest}")
                self._emit("queue_changed")
            except Exception as e:
                self._emit("status", f"Could not copy torrent file: {e}")
            return

        h = item.handle
        if h is None:
            return

        try:
            if not h.status().has_metadata:
                return

            ti = (h.torrent_file_with_hashes()
                  if hasattr(h, "torrent_file_with_hashes")
                  else h.torrent_file())
            if ti is None:
                return

            dest = os.path.abspath(
                self._torrent_descriptor_path(item, ti.name() or item.name)
            )

            encoded = None

            # Newer libtorrent bindings.
            if hasattr(lt, "write_torrent_file"):
                try:
                    params = lt.add_torrent_params()
                    params.ti = ti

                    trackers, tiers = [], []
                    try:
                        for tr in h.trackers():
                            if isinstance(tr, dict):
                                url = tr.get("url", "")
                                tier = int(tr.get("tier", 0))
                            else:
                                url = getattr(tr, "url", "")
                                tier = int(getattr(tr, "tier", 0))
                            if url:
                                trackers.append(url)
                                tiers.append(tier)
                    except Exception:
                        pass

                    try:
                        params.trackers = trackers
                        params.tracker_tiers = tiers
                    except Exception:
                        pass

                    encoded = lt.bencode(lt.write_torrent_file(params))
                except Exception:
                    encoded = None

            # Compatibility fallback.
            if encoded is None:
                ct = lt.create_torrent(ti)
                try:
                    for tr in h.trackers():
                        if isinstance(tr, dict):
                            url = tr.get("url", "")
                            tier = int(tr.get("tier", 0))
                        else:
                            url = getattr(tr, "url", "")
                            tier = int(getattr(tr, "tier", 0))
                        if url:
                            try:
                                ct.add_tracker(url, tier)
                            except TypeError:
                                ct.add_tracker(url)
                except Exception:
                    pass
                encoded = lt.bencode(ct.generate())

            with open(dest, "wb") as f:
                f.write(encoded)

            item.torrent_file_path = dest
            item.torrent_file_saved = True
            self._emit("status", f"Saved torrent file: {dest}")
            self._emit("queue_changed")
        except Exception as e:
            self._emit("status", f"Could not save torrent metadata yet: {e}")

    def add_failed_placeholder(self, source, save_path, torrent_dir, error_message):
        """Create/retain a persistent failed queue item without duplicating rows."""
        identity = torrent_identity_from_source(source)
        existing = self._existing_identity_item(identity)
        if existing is not None:
            existing.error = str(error_message or "Download failed.")
            if existing.state != "finished":
                existing.state = "error"
            self._emit(
                "status_detail",
                {
                    "level": "ERROR",
                    "message": (
                        f"Existing torrent entry retained after failure: "
                        f"{existing.name} | {existing.error}"
                    ),
                },
            )
            self._emit("torrents")
            self._emit("queue_changed")
            return existing

        item = TorrentItem(source, save_path, torrent_dir)
        item.identity = identity
        item.state = "error"
        item.error = str(error_message or "Download failed.")

        try:
            if (
                source
                and not source.lower().startswith("magnet:")
                and os.path.isfile(source)
            ):
                info = lt.torrent_info(source)
                if info.name():
                    item.name = info.name()
                item.total = int(info.total_size())
                if source.lower().endswith(".torrent"):
                    item.torrent_file_path = os.path.abspath(source)
                    item.torrent_file_saved = True
        except Exception:
            pass

        with self.lock:
            self.items[item.uid] = item

        self._emit(
            "status_detail",
            {
                "level": "ERROR",
                "message": (
                    f"Torrent remains in the queue as Failed: {item.name} | "
                    f"{item.error}"
                ),
            },
        )
        self._emit("torrents")
        self._emit("queue_changed")
        return item

    def add_torrent(self, source, save_path, torrent_dir):
        """Add exactly one swarm to exactly one application row."""
        identity = torrent_identity_from_source(source)

        # Serialize all adds. Browser/IPC double launches can otherwise race
        # each other before either new row has reached self.items.
        with self._add_lock:
            existing = self._existing_identity_item(identity)
            if existing is not None:
                self._emit(
                    "status_detail",
                    {
                        "level": "TORRENT",
                        "message": (
                            f"Torrent is already in the list; duplicate browser/file add "
                            f"ignored: {existing.name}"
                        ),
                    },
                )
                self._emit("duplicate_torrent", {"uid": existing.uid})
                return existing

            item = TorrentItem(source, save_path, torrent_dir)
            item.identity = identity

            try:
                log_startup_diagnostic(
                    "engine.add_torrent: preparing add_torrent_params"
                )
                if source.startswith("magnet:"):
                    log_startup_diagnostic(
                        "engine.add_torrent: calling libtorrent.parse_magnet_uri"
                    )
                    atp = lt.parse_magnet_uri(source)
                    log_startup_diagnostic(
                        "engine.add_torrent: parse_magnet_uri returned"
                    )
                    atp.save_path = save_path
                else:
                    info = lt.torrent_info(source)
                    atp = lt.add_torrent_params()
                    atp.ti = info
                    atp.save_path = save_path
                    item.name = info.name()

                # Queueing is intentional and controlled by active_downloads.
                # duplicate_is_error prevents libtorrent from silently returning
                # a handle to an existing torrent and letting two GUI rows alias
                # the same live torrent.
                atp.flags &= ~lt.torrent_flags.seed_mode
                atp.flags &= ~lt.torrent_flags.upload_mode
                atp.flags |= lt.torrent_flags.auto_managed
                atp.flags |= lt.torrent_flags.paused
                atp.flags |= lt.torrent_flags.duplicate_is_error

                log_startup_diagnostic(
                    "engine.add_torrent: calling session.add_torrent"
                )
                handle = self.session.add_torrent(atp)
                log_startup_diagnostic(
                    "engine.add_torrent: session.add_torrent returned"
                )

                # Defensive second guard: a binding/version should never return
                # an existing handle with duplicate_is_error, but do not permit
                # handle aliasing even if it does.
                with self.lock:
                    for existing_item in self.items.values():
                        try:
                            same_handle = (
                                existing_item.handle is not None
                                and existing_item.handle == handle
                            )
                        except Exception:
                            same_handle = False
                        if same_handle:
                            self._emit(
                                "status_detail",
                                {
                                    "level": "TORRENT",
                                    "message": (
                                        "Libtorrent returned an existing torrent "
                                        f"handle; duplicate row suppressed: "
                                        f"{existing_item.name}"
                                    ),
                                },
                            )
                            self._emit(
                                "duplicate_torrent",
                                {"uid": existing_item.uid},
                            )
                            return existing_item

                item.handle = handle
                item.handle.set_upload_mode(False)

                with self.lock:
                    self.items[item.uid] = item

                self._save_torrent_descriptor(item)
                source_kind = (
                    "magnet" if source.startswith("magnet:")
                    else ".torrent file"
                )
                self._emit(
                    "status_detail",
                    {
                        "level": "TORRENT",
                        "message": (
                            f"Added {source_kind}: {item.name}. "
                            f"Data folder: {save_path}"
                        ),
                    },
                )

            except Exception as e:
                # If the session itself reports a duplicate, look up our
                # identity again instead of making a second failed row.
                existing = self._existing_identity_item(identity)
                if existing is not None:
                    self._emit(
                        "status_detail",
                        {
                            "level": "TORRENT",
                            "message": (
                                f"Duplicate torrent add ignored: {existing.name}"
                            ),
                        },
                    )
                    self._emit("duplicate_torrent", {"uid": existing.uid})
                    return existing

                item.state = "error"
                item.error = f"{type(e).__name__}: {e}"
                with self.lock:
                    self.items[item.uid] = item

                source_kind = (
                    "magnet"
                    if source.startswith("magnet:")
                    else "torrent-file"
                )
                log_path = log_external_error(
                    "libtorrent failed while adding torrent source.",
                    exc=e,
                    details=(
                        f"source_kind={source_kind}\\n"
                        f"source={source!r}\\n"
                        f"save_path={save_path!r}\\n"
                        f"torrent_dir={torrent_dir!r}"
                    ),
                )

                self._emit(
                    "error",
                    (
                        f"Failed to add torrent: {item.error}"
                        + (
                            f" | Diagnostic log: {log_path}"
                            if log_path
                            else ""
                        )
                    ),
                )

            self._emit("torrents")
            self._emit("queue_changed")
            return item

    def reload_torrent(self, uid, force_recheck=False):
        """Restart an existing torrent without deleting any payload data.

        Finished torrents can be re-added and force-rechecked. Active torrents
        are restarted cleanly. The old GUI row is replaced by a new row, which
        also makes an explicitly reloaded torrent the newest entry.
        """
        with self._add_lock:
            with self.lock:
                old_item = self.items.get(uid)

            if old_item is None:
                return None

            original_source = old_item.source
            descriptor = old_item.torrent_file_path
            add_source = ""
            if descriptor and os.path.isfile(descriptor):
                add_source = descriptor
            elif original_source:
                add_source = original_source

            if not add_source:
                self._emit(
                    "error",
                    f"Cannot reload {old_item.name}: torrent source is missing.",
                )
                return None

            old_name = old_item.name
            old_save_path = old_item.save_path
            old_torrent_dir = old_item.torrent_dir
            old_descriptor = old_item.torrent_file_path

            if old_item.handle is not None and self.session is not None:
                try:
                    old_item.handle.pause()
                except Exception:
                    pass
                try:
                    # No delete_files flag: all downloaded/partial data stays.
                    self.session.remove_torrent(old_item.handle)
                except Exception:
                    pass
                old_item.handle = None

            with self.lock:
                self.items.pop(uid, None)

            new_item = self.add_torrent(
                add_source,
                old_save_path,
                old_torrent_dir,
            )
            if new_item is None:
                return None

            # Keep the original user-facing source/descriptor information.
            new_item.source = original_source or add_source
            if old_name:
                new_item.name = old_name
            if old_descriptor:
                new_item.torrent_file_path = old_descriptor
                new_item.torrent_file_saved = os.path.isfile(old_descriptor)

            if (
                force_recheck
                and new_item.handle is not None
            ):
                try:
                    new_item.handle.force_recheck()
                    new_item.state = "queued"
                    self._emit(
                        "status_detail",
                        {
                            "level": "TORRENT",
                            "message": (
                                f"Rechecking existing downloaded data: "
                                f"{new_item.name}. Missing/corrupt pieces will "
                                f"be downloaded again; existing files are not "
                                f"deleted."
                            ),
                        },
                    )
                except Exception as e:
                    self._emit(
                        "error",
                        f"Could not force recheck {new_item.name}: {e}",
                    )
            else:
                self._emit(
                    "status_detail",
                    {
                        "level": "TORRENT",
                        "message": (
                            f"Reloaded existing torrent without deleting data: "
                            f"{new_item.name}"
                        ),
                    },
                )

            self._emit("torrents")
            self._emit("queue_changed")
            return new_item

    def pause_torrents(self, uids):
        with self.lock:
            items = [self.items.get(uid) for uid in uids]
        for item in items:
            if item is None or item.handle is None:
                continue
            if item.state not in ("downloading", "metadata"):
                continue
            try:
                try:
                    item.handle.unset_flags(lt.torrent_flags.auto_managed)
                except Exception:
                    pass
                item.handle.pause()
                item.state = "paused"
                self._emit(
                    "status_detail",
                    {"level": "TORRENT", "message": f"Paused: {item.name}"},
                )
            except Exception as e:
                self._emit("error", f"Could not pause {item.name}: {e}")
        self._emit("torrents")
        self._emit("queue_changed")

    def resume_torrents(self, uids):
        with self.lock:
            items = [self.items.get(uid) for uid in uids]
        for item in items:
            if item is None or item.handle is None or item.state != "paused":
                continue
            try:
                # Return it to libtorrent's queue manager. If a slot is free it
                # starts immediately; otherwise the GUI explicitly shows Queued.
                item.handle.set_flags(lt.torrent_flags.auto_managed)
                item.handle.pause()
                item.handle.set_upload_mode(False)
                item.state = "queued"
                self._last_progress_time[item.uid] = (item.progress, time.time())
                self._emit(
                    "status_detail",
                    {"level": "TORRENT", "message": f"Resumed: {item.name}"},
                )
            except Exception as e:
                self._emit("error", f"Could not resume {item.name}: {e}")
        self._emit("torrents")
        self._emit("queue_changed")

    def _payload_relative_paths(self, item):
        paths = []
        try:
            if item.handle is not None:
                ti = item.handle.torrent_file()
                if ti is not None:
                    fs = ti.files()
                    for i in range(fs.num_files()):
                        try:
                            paths.append(fs.file_path(i))
                        except Exception:
                            paths.append(fs.file_name(i))
        except Exception:
            pass
        if not paths:
            for f in item.files:
                path = f.get("path") or f.get("name")
                if path:
                    paths.append(path)
        return paths

    def _delete_payload_paths(self, save_path, relative_paths):
        base = os.path.abspath(save_path)
        safe_files = []
        for rel in relative_paths:
            try:
                candidate = os.path.abspath(os.path.join(base, rel))
                if os.path.commonpath([base, candidate]) != base:
                    continue
                safe_files.append(candidate)
            except Exception:
                continue

        for _attempt in range(12):
            remaining = []
            for path in safe_files:
                if not os.path.exists(path):
                    continue
                try:
                    if os.path.isfile(path) or os.path.islink(path):
                        os.remove(path)
                    else:
                        remaining.append(path)
                except Exception:
                    remaining.append(path)
            safe_files = remaining
            if not safe_files:
                break
            time.sleep(0.25)

        dirs = set()
        for rel in relative_paths:
            try:
                candidate = os.path.abspath(os.path.join(base, rel))
                if os.path.commonpath([base, candidate]) != base:
                    continue
                d = os.path.dirname(candidate)
                while d and d != base and os.path.commonpath([base, d]) == base:
                    dirs.add(d)
                    parent = os.path.dirname(d)
                    if parent == d:
                        break
                    d = parent
            except Exception:
                pass
        for d in sorted(dirs, key=len, reverse=True):
            try:
                os.rmdir(d)
            except Exception:
                pass

    def remove_torrent(self, uid, delete_data=False, delete_descriptor=True):
        with self.lock:
            item = self.items.pop(uid, None)
            self._last_progress_time.pop(uid, None)

        if item is None:
            return

        payload_paths = self._payload_relative_paths(item) if delete_data else []

        if item.handle is not None and self.session is not None:
            try:
                item.handle.pause()
            except Exception:
                pass

            removed_with_delete_flag = False
            if delete_data:
                try:
                    delete_flag = getattr(getattr(lt, "options_t", None), "delete_files", None)
                    if delete_flag is not None:
                        self.session.remove_torrent(item.handle, delete_flag)
                        removed_with_delete_flag = True
                except Exception:
                    removed_with_delete_flag = False

            if not removed_with_delete_flag:
                try:
                    self.session.remove_torrent(item.handle)
                except Exception:
                    pass

        if delete_descriptor and item.torrent_file_path:
            try:
                if os.path.isfile(item.torrent_file_path):
                    os.remove(item.torrent_file_path)
            except Exception as e:
                self._emit("status", f"Could not delete torrent file: {e}")

        if delete_data and payload_paths:
            threading.Thread(
                target=self._delete_payload_paths,
                args=(item.save_path, payload_paths),
                daemon=True,
            ).start()

        self._emit(
            "status_detail",
            {
                "level": "TORRENT",
                "message": (
                    f"Removed {item.name}"
                    + (" and requested permanent data deletion." if delete_data
                       else "; downloaded data was kept.")
                ),
            },
        )
        self._emit("torrents")
        self._emit("queue_changed")

    def stop(self):
        self.stop_flag.set()
        self._running = False

    def _detach_finished_torrent(self, item):
        """Keep a finished torrent in the app but remove its network handle.

        remove_torrent() without delete_files removes only libtorrent's live
        session object. The downloaded payload and saved .torrent file remain.
        """
        h = item.handle
        if h is None:
            return

        try:
            h.pause()
        except Exception:
            pass

        if self.session is not None:
            try:
                self.session.remove_torrent(h)
            except Exception:
                # If the binding refuses removal for any reason, keeping the
                # paused handle is safer than touching downloaded data.
                return

        item.handle = None
        item.dl_rate = 0.0
        item.network_rate = 0.0
        item.peers = 0
        self._last_progress_time.pop(item.uid, None)
        self._emit(
            "status_detail",
            {
                "level": "OK",
                "message": (
                    f"Finished torrent moved to offline list state: {item.name}. "
                    "No further tracker/DHT/peer checks are needed."
                ),
            },
        )
        self._emit("queue_changed")

    def _refresh_files(self, item):
        h = item.handle
        st = h.status()
        if not st.has_metadata:
            return
        ti = h.torrent_file()
        if ti is None:
            return
        fs = ti.files()
        item.piece_len = ti.piece_length()
        num_files = fs.num_files()

        try:
            file_prog = h.file_progress()
        except Exception:
            file_prog = [0] * num_files

        try:
            item.piece_map = list(st.pieces)      # list[bool]
        except Exception:
            item.piece_map = []

        files = []
        for i in range(num_files):
            size = fs.file_size(i)
            offset = fs.file_offset(i)
            done = file_prog[i] if i < len(file_prog) else 0
            prog = (done / size) if size else 1.0
            first_piece = offset // item.piece_len if item.piece_len else 0
            last_byte = offset + max(size - 1, 0)
            last_piece = last_byte // item.piece_len if item.piece_len else 0
            try:
                relative_path = fs.file_path(i)
            except Exception:
                relative_path = fs.file_name(i)
            files.append({
                "name": fs.file_name(i),
                "path": relative_path,
                "size": size,
                "progress": prog,
                "first_piece": int(first_piece),
                "last_piece": int(last_piece),
            })
        item.files = files

    def _run_guarded(self):
        """Never let an unexpected engine exception disappear silently."""
        try:
            self._run()
        except Exception as e:
            self._running = False
            log_path = log_external_error(
                "Unhandled torrent engine worker exception.",
                exc=e,
            )
            try:
                self._emit(
                    "status_detail",
                    {
                        "level": "ERROR",
                        "message": (
                            "Torrent engine error: "
                            f"{type(e).__name__}: {e}. "
                            "The GUI will remain open."
                            + (
                                f" Diagnostic log: {log_path}"
                                if log_path
                                else ""
                            )
                        ),
                    },
                )
                self._emit(
                    "error",
                    (
                        "Torrent engine stopped because of an unexpected "
                        f"error: {type(e).__name__}: {e}"
                    ),
                )
                self._emit("torrents")
            except Exception:
                pass

    def _run(self):
        while not self.stop_flag.is_set():
            with self.lock:
                items = list(self.items.values())

            any_active = False
            for item in items:
                if item.handle is None:
                    continue
                s = item.handle.status()
                try:
                    # upload_mode=False is required for DOWNLOADING. Piece uploads
                    # are prevented separately with zero global unchoke slots.
                    item.handle.set_upload_mode(False)
                except Exception:
                    pass

                # `torrent_status.progress` is ambiguous during magnet
                # metadata/discovery and checking transitions. In particular,
                # libtorrent may transiently report 1.0 while total_wanted is
                # still zero, which made the UI show a fake 100% before any
                # file payload existed. The app's Progress column means actual
                # wanted FILE BYTES present on disk, so calculate it ourselves.
                has_metadata = bool(
                    getattr(s, "has_metadata", False)
                )
                wanted_total = max(
                    0,
                    int(getattr(s, "total_wanted", 0) or 0),
                )
                wanted_done = max(
                    0,
                    int(getattr(s, "total_wanted_done", 0) or 0),
                )

                if has_metadata and wanted_total > 0:
                    item.progress = max(
                        0.0,
                        min(1.0, wanted_done / float(wanted_total)),
                    )
                else:
                    # Metadata/discovery is not file-download progress.
                    item.progress = 0.0

                # `download_rate` includes protocol chatter. Only
                # `download_payload_rate` represents actual file data.
                item.network_rate = float(
                    getattr(s, "download_rate", 0.0) or 0.0
                )
                item.dl_rate = float(
                    getattr(
                        s,
                        "download_payload_rate",
                        0.0,
                    )
                    or 0.0
                )
                item.payload_total = int(
                    getattr(
                        s,
                        "total_payload_download",
                        0,
                    )
                    or 0
                )

                item.total = wanted_total
                item.downloaded = min(wanted_done, wanted_total) if wanted_total > 0 else 0
                item.peers = s.num_peers
                if has_metadata:
                    try:
                        ti = item.handle.torrent_file()
                        if ti is not None and ti.name():
                            item.name = ti.name()
                    except Exception:
                        pass
                    if not item.torrent_file_saved:
                        self._save_torrent_descriptor(item)

                try:
                    flags = item.handle.flags()
                    auto_managed = bool(
                        flags & lt.torrent_flags.auto_managed
                    )
                    is_paused = bool(
                        flags & lt.torrent_flags.paused
                    )
                except Exception:
                    auto_managed = False
                    is_paused = False

                payload_complete = (
                    has_metadata
                    and wanted_total > 0
                    and wanted_done >= wanted_total
                )

                if bool(getattr(s, "is_finished", False)) or payload_complete:
                    if item.state != "finished":
                        self._emit(
                            "status_detail",
                            {
                                "level": "OK",
                                "message": f"Finished: {item.name}",
                            },
                        )
                        self._emit("queue_changed")
                    item.state = "finished"
                    try:
                        item.handle.pause()
                    except Exception:
                        pass
                elif item.state in ("paused", "stopped") and not auto_managed:
                    # User-controlled paused/stopped state wins.
                    pass
                elif auto_managed and is_paused:
                    item.state = "queued"
                elif not has_metadata:
                    item.state = "metadata"
                    any_active = True
                    if not self._metadata_discovery_tick(item, s):
                        # It has been detached and persisted as Failed.
                        continue
                else:
                    # Metadata arrived. Clear metadata retry state, then apply
                    # the separate three-attempt usable-peer policy.
                    self._metadata_discovery_tick(item, s)
                    item.state = "downloading"
                    any_active = True
                    if not self._peer_discovery_tick(item, s):
                        # Converted to persistent Failed and detached.
                        continue

                had_files = bool(item.files)
                self._refresh_files(item)

                if has_metadata and item.files and not had_files:
                    self._emit(
                        "status_detail",
                        {
                            "level": "OK",
                            "message": (
                                f"Metadata ready: {item.name} | "
                                f"{len(item.files)} file(s) | {human_size(item.total)}"
                            ),
                        },
                    )

                # A completed torrent no longer needs a live network object.
                # Capture its local metadata above, detach it, and never run
                # tracker/DHT/peer diagnostics for the finished queue entry.
                if item.state == "finished":
                    self._detach_finished_torrent(item)
                    continue

                # Metadata discovery has its own compact Attempt N/3 log.
                # Do not repeat an identical FETCHING METADATA diagnostic every
                # few seconds or expose every tracker URL it cycles through.
                if (
                    item.state != "metadata"
                    and not (
                        item.state == "downloading"
                        and item.peer_attempt > 0
                        and item.dl_rate <= 0
                    )
                ):
                    self._torrent_diagnostic(item, s)

                if self.use_proxy and item.state in ("downloading", "metadata"):
                    now = time.time()
                    prev = self._last_progress_time.get(item.uid)
                    if prev is None or item.progress > prev[0] + 1e-6:
                        self._last_progress_time[item.uid] = (item.progress, now)

            if self.use_proxy and any_active:
                now = time.time()

                # Re-check the actual active proxy every few seconds. A dead
                # proxy is now detected in seconds instead of waiting for a
                # torrent progress timeout.
                if now - self._last_proxy_health_check >= self.PROXY_HEALTH_INTERVAL:
                    self._last_proxy_health_check = now
                    current_proxy = self.proxy_mgr.current()
                    healthy, health_detail = self._quick_proxy_health(current_proxy)

                    if not healthy:
                        dead_key = self.proxy_mgr.current_key()
                        self.proxy_mgr.set_status(dead_key, "failed")
                        self._emit(
                            "status",
                            f"Proxy {dead_key} failed its health check. Switching..."
                        )
                        self.proxy_mgr.advance()
                        nxt = self._select_working_proxy()

                        if nxt is None:
                            self._emit("proxy")
                            self._emit(
                                "error",
                                "No working proxy remains. Downloading stopped; "
                                "the app did not fall back to the direct connection."
                            )
                            for item in items:
                                if item.handle is not None:
                                    try:
                                        item.handle.pause()
                                    except Exception:
                                        pass
                                    item.state = "stopped"
                            self._emit("torrents")
                            self._running = False
                            return

                        if not self._apply_proxy(nxt, reset_connections=True):
                            self._emit(
                                "error",
                                "Could not apply the next working proxy. Downloading stopped."
                            )
                            for item in items:
                                if item.handle is not None:
                                    try:
                                        item.handle.pause()
                                    except Exception:
                                        pass
                                    item.state = "stopped"
                            self._running = False
                            return

                        now2 = time.time()
                        for item in items:
                            self._last_progress_time[item.uid] = (item.progress, now2)
                        self._last_proxy_health_check = now2
                        continue

                # Magnet metadata has no normal percentage progress. If there
                # are no payload downloads yet and metadata still has not
                # arrived after a short window, try the next proxy. This does
                # not claim the old proxy is dead; it may simply be unsuitable
                # for this swarm/peer traffic.
                metadata_items = [it for it in items if it.state == "metadata"]
                downloading_items = [it for it in items if it.state == "downloading"]

                if metadata_items and not downloading_items:
                    timed_out = True
                    for item in metadata_items:
                        _prog, t = self._last_progress_time.get(item.uid, (0, now))
                        if now - t < self.METADATA_PROXY_TIMEOUT:
                            timed_out = False
                            break

                    if timed_out:
                        old_key = self.proxy_mgr.current_key()
                        self.proxy_mgr.set_status(old_key, "no metadata")
                        self._emit(
                            "status",
                            f"No magnet metadata after {self.METADATA_PROXY_TIMEOUT:.0f}s "
                            f"through {old_key}. Trying the next proxy..."
                        )
                        self.proxy_mgr.advance()
                        nxt = self._select_working_proxy()

                        if nxt is None:
                            self._emit("proxy")
                            self._emit(
                                "error",
                                "Every proxy was tried but magnet metadata never arrived. "
                                "The proxies may restrict BitTorrent peer connections, or "
                                "the torrent may not have reachable metadata peers."
                            )
                            for item in metadata_items:
                                if item.handle is not None:
                                    item.metadata_attempt = (
                                        self.METADATA_MAX_ATTEMPTS
                                    )
                                    self._fail_metadata_torrent(item)
                            self._emit("torrents")
                            continue

                        if not self._apply_proxy(nxt, reset_connections=True):
                            self._emit(
                                "error",
                                "Could not apply the next proxy. Downloading stopped."
                            )
                            self._running = False
                            return

                        now2 = time.time()
                        for item in metadata_items:
                            self._last_progress_time[item.uid] = (item.progress, now2)
                        self._last_proxy_health_check = now2

            if any_active:
                self._drain_alerts()
            elif self.session is not None:
                # Finished/paused-only sessions do not need network chatter in
                # App Status. Discard any already-queued stop/tracker alerts so
                # they cannot appear later when another torrent is started.
                try:
                    self.session.pop_alerts()
                except Exception:
                    pass

            self._emit("torrents")
            time.sleep(1.0)

        self._running = False


# ---------------------------------------------------------------------------
# Canvas-drawn piece map: segments shaded where pieces are downloaded
# ---------------------------------------------------------------------------
class PieceMapBar(tk.Canvas):
    def __init__(self, master, height=18, **kw):
        super().__init__(master, height=height, highlightthickness=1,
                         highlightbackground="#4b5864", bg="#313a43", **kw)
        self._pieces = []
        self.bind("<Configure>", self.redraw)

    def set_pieces(self, have_list):
        self._pieces = have_list or []
        self.redraw()

    def redraw(self, event=None):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 1:
            return
        n = len(self._pieces)
        if n == 0:
            self.create_text(w / 2, h / 2, text="no data",
                             fill="#9da9b3", font=("Segoe UI", 12))
            return
        # collapse to at most ~w segments so very large piece counts still draw
        max_seg = max(1, int(w))
        if n > max_seg:
            step = n / max_seg
            buckets = []
            for k in range(max_seg):
                a = int(k * step)
                b = max(a + 1, int((k + 1) * step))
                chunk = self._pieces[a:b]
                buckets.append(any(chunk))
            draw = buckets
        else:
            draw = self._pieces
        m = len(draw)
        seg = w / m
        for i, have in enumerate(draw):
            x0 = i * seg
            x1 = (i + 1) * seg
            self.create_rectangle(x0, 0, x1, h,
                                  fill="#33cc55" if have else "#3d4852",
                                  outline="")


# ---------------------------------------------------------------------------
# Canvas-drawn ON/OFF toggle switch (ttk has no native switch)
# ---------------------------------------------------------------------------
class ToggleSwitch(tk.Canvas):
    def __init__(self, master, variable, command=None, scale=1.0, **kw):
        self.scale = scale
        self.w = int(52 * scale)
        self.h = int(28 * scale)
        super().__init__(
            master,
            width=self.w,
            height=self.h,
            highlightthickness=0,
            bd=0,
            bg="#252c33",
            cursor="hand2",
            **kw,
        )
        self.var = variable
        self.command = command
        self.bind("<Button-1>", self._toggle)
        try:
            self.var.trace_add("write", lambda *a: self._draw())
        except Exception:
            pass
        self._draw()

    def _toggle(self, event=None):
        self.var.set(not self.var.get())
        self._draw()
        if self.command:
            self.command()

    def _draw(self):
        self.delete("all")
        on = bool(self.var.get())
        pad = int(2 * self.scale)
        r = (self.h - 2 * pad) / 2
        track = "#33aa55" if on else "#888888"
        # rounded track
        self.create_oval(pad, pad, pad + 2 * r, pad + 2 * r, fill=track, outline="")
        self.create_oval(self.w - pad - 2 * r, pad, self.w - pad, pad + 2 * r,
                         fill=track, outline="")
        self.create_rectangle(pad + r, pad, self.w - pad - r, pad + 2 * r,
                              fill=track, outline="")
        # knob
        kx = (self.w - pad - 2 * r) if on else pad
        self.create_oval(kx, pad, kx + 2 * r, pad + 2 * r,
                         fill="#ffffff", outline="#dddddd")
        label = "ON" if on else "OFF"
        tx = self.w * 0.32 if on else self.w * 0.68
        toggle_font_size = max(12, int(round(12 * min(self.scale, 1.35))))
        self.create_text(tx, self.h / 2, text=label,
                         fill="#ffffff" if on else "#eeeeee",
                         font=("Segoe UI", toggle_font_size, "bold"))


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class App(tk.Tk):
    THEMES = {
        "Slate Night": {
            "bg": "#252c33", "panel": "#2d353d", "panel_alt": "#343e47",
            "field": "#313a43", "field_alt": "#39434d", "border": "#4b5864",
            "text": "#edf2f6", "muted": "#bcc6cf", "accent": "#5b8fbd",
            "accent2": "#65d4c8", "select": "#42627d", "button": "#394650",
            "button_active": "#465661", "green": "#33cc55", "error": "#6a343a",
            "warning": "#65552d", "metadata": "#30495b",
        },
        "Ocean Depths": {
            "bg": "#071b2a", "panel": "#0b2738", "panel_alt": "#10364a",
            "field": "#0d3042", "field_alt": "#17465b", "border": "#28637a",
            "text": "#e8f8ff", "muted": "#9fc8d8", "accent": "#20a4d8",
            "accent2": "#43e0d0", "select": "#176b8b", "button": "#12415a",
            "button_active": "#185875", "green": "#2ed97b", "error": "#7b3044",
            "warning": "#7a6228", "metadata": "#123f5a",
        },
        "Forest Glow": {
            "bg": "#102019", "panel": "#173027", "panel_alt": "#1e4033",
            "field": "#19372c", "field_alt": "#25503f", "border": "#3c6c58",
            "text": "#effbf3", "muted": "#b6d7c3", "accent": "#4fb979",
            "accent2": "#b7e64a", "select": "#32754e", "button": "#28513f",
            "button_active": "#34694f", "green": "#52e06f", "error": "#76373d",
            "warning": "#756326", "metadata": "#244b42",
        },
        "Sunset Drive": {
            "bg": "#2a1822", "panel": "#3a2030", "panel_alt": "#4b293c",
            "field": "#402437", "field_alt": "#583049", "border": "#76475e",
            "text": "#fff3f3", "muted": "#e0b8c5", "accent": "#ff6b6b",
            "accent2": "#ffb347", "select": "#9b465b", "button": "#5b3045",
            "button_active": "#743d52", "green": "#5be68b", "error": "#823840",
            "warning": "#8a682b", "metadata": "#51304d",
        },
        "80s Pinball": {
            "bg": "#13071f", "panel": "#24103b", "panel_alt": "#351352",
            "field": "#2b1044", "field_alt": "#461568", "border": "#7d38a6",
            "text": "#fff6ff", "muted": "#d9b8eb", "accent": "#ff2fa8",
            "accent2": "#20e7ff", "select": "#7430a0", "button": "#4b1768",
            "button_active": "#6a208c", "green": "#55ff75", "error": "#9c234c",
            "warning": "#ad7321", "metadata": "#33236a",
        },
        "Miami Neon": {
            "bg": "#091b28", "panel": "#10283a", "panel_alt": "#17374a",
            "field": "#122f43", "field_alt": "#1f4358", "border": "#2f7181",
            "text": "#f8fbff", "muted": "#b3d4dc", "accent": "#ff4fb8",
            "accent2": "#35f2e4", "select": "#7a3d83", "button": "#24485c",
            "button_active": "#315e73", "green": "#55ed8a", "error": "#8a3152",
            "warning": "#8e6b27", "metadata": "#1c4660",
        },
        "Cyber Purple": {
            "bg": "#171323", "panel": "#211b34", "panel_alt": "#2d2445",
            "field": "#271f3b", "field_alt": "#382b52", "border": "#5c4b78",
            "text": "#f5f0ff", "muted": "#cbbde0", "accent": "#9a6cff",
            "accent2": "#d85cff", "select": "#634995", "button": "#3b2e56",
            "button_active": "#4e3b70", "green": "#52dc83", "error": "#78384f",
            "warning": "#78622f", "metadata": "#34315a",
        },
        "Arcade Blue": {
            "bg": "#071529", "panel": "#0d2240", "panel_alt": "#12315a",
            "field": "#102b4e", "field_alt": "#173b68", "border": "#2d5f91",
            "text": "#eef7ff", "muted": "#adc9e6", "accent": "#2f8fff",
            "accent2": "#7c5cff", "select": "#245ea0", "button": "#173d69",
            "button_active": "#205287", "green": "#47df7a", "error": "#76374f",
            "warning": "#7d642b", "metadata": "#173b67",
        },
        "Cherry Voltage": {
            "bg": "#241013", "panel": "#34171c", "panel_alt": "#472028",
            "field": "#3c1a20", "field_alt": "#54242c", "border": "#7d3c48",
            "text": "#fff3f4", "muted": "#dfb8be", "accent": "#ef476f",
            "accent2": "#ffd166", "select": "#983a51", "button": "#58252e",
            "button_active": "#71303b", "green": "#54dc7e", "error": "#8c2e3b",
            "warning": "#8a6726", "metadata": "#4a2537",
        },
        "Solar Flare": {
            "bg": "#241b0e", "panel": "#342815", "panel_alt": "#46371b",
            "field": "#3c2f17", "field_alt": "#57431e", "border": "#80652c",
            "text": "#fff9e9", "muted": "#e4d1a2", "accent": "#ff9f1c",
            "accent2": "#ffe66d", "select": "#9a681f", "button": "#5c451e",
            "button_active": "#765927", "green": "#65de72", "error": "#813b35",
            "warning": "#947326", "metadata": "#4a3c25",
        },
    }

    COLUMN_MIN_WIDTHS = {
        "idx": 42,
        "name": 110,
        "size": 78,
        "progress": 120,
        "speed": 82,
        "eta": 88,
        "peers": 58,
        "status": 92,
    }
    COLUMNS = [
        ("idx", "#", 50),
        ("name", "Name", 340),
        ("size", "Size", 110),
        ("progress", "Progress", 210),
        ("speed", "Speed", 120),
        ("eta", "ETA", 105),
        ("peers", "Peers", 70),
        ("status", "Status", 120),
    ]

    def __init__(self, scale):
        super().__init__()
        self.title("GetTorrent")

        try:
            self.iconbitmap(resource_path("GetTorrent.ico"))
        except Exception:
            pass

        # IMPORTANT: font point sizes are interpreted using Tk's scaling value.
        # The previous build forced 96 DPI even on a high-DPI Windows display,
        # which made 12/13pt text physically tiny. Use the DPI Windows actually
        # assigned to this window.
        self.window_dpi = window_dpi(self, scale)
        self.dpi_scale = max(1.0, self.window_dpi / 96.0)

        try:
            self.tk.call("tk", "scaling", self.window_dpi / 72.0)
        except Exception:
            pass

        # Keep geometric column multipliers modest. Fonts/buttons scale through
        # Tk's real DPI; doubling every column width as well would waste space.
        self.scale = max(1.0, min(self.dpi_scale, 1.15))

        # UI font policy: NO application text below 12pt.
        # Main controls: 13pt. Secondary/diagnostic/mono: 12pt.
        base = 13
        self.font_normal = ("Segoe UI", base)
        self.font_bold = ("Segoe UI", base, "bold")
        self.font_mono = ("Consolas", 12)
        self.font_small = ("Segoe UI", 12)

        # Tk/ttk contains widgets that use named default fonts instead of the
        # explicit style font. Configure every standard named font so LabelFrame
        # captions, menus, comboboxes, entry widgets, etc. cannot fall back to
        # tiny 8/9pt defaults.
        named_font_specs = {
            "TkDefaultFont": ("Segoe UI", 13, "normal"),
            "TkTextFont": ("Segoe UI", 13, "normal"),
            "TkMenuFont": ("Segoe UI", 12, "normal"),
            "TkHeadingFont": ("Segoe UI", 13, "bold"),
            "TkCaptionFont": ("Segoe UI", 13, "bold"),
            "TkSmallCaptionFont": ("Segoe UI", 12, "normal"),
            "TkIconFont": ("Segoe UI", 12, "normal"),
            "TkFixedFont": ("Consolas", 12, "normal"),
        }
        for font_name, (family, size, weight) in named_font_specs.items():
            try:
                tkfont.nametofont(font_name).configure(
                    family=family,
                    size=size,
                    weight=weight,
                )
            except Exception:
                pass

        # Also provide a global fallback for classic Tk widgets.
        self.option_add("*Font", "{Segoe UI} 13")
        self.option_add("*Menu.Font", "{Segoe UI} 12")
        self.option_add("*Text.Font", "{Consolas} 12")
        self.option_add("*Listbox.Font", "{Segoe UI} 13")

        self.settings = load_app_settings()
        self.theme_name = str(
            self.settings.get("theme", "Slate Night") or "Slate Night"
        )
        if self.theme_name not in self.THEMES:
            self.theme_name = "Slate Night"
        self._column_resize_state = None

        sw = max(1024, self.winfo_screenwidth())
        sh = max(720, self.winfo_screenheight())
        w = min(1500, max(1100, int(sw * 0.94)))
        h = min(920, max(720, int(sh * 0.92)))

        saved_geometry = self.settings.get("window_geometry", "")
        if saved_geometry:
            try:
                self.geometry(saved_geometry)
            except Exception:
                self.geometry(f"{w}x{h}")
        else:
            self.geometry(f"{w}x{h}")

        self.minsize(min(1050, w), min(700, h))
        self._last_visible_state = self.settings.get(
            "window_state", "zoomed"
        )
        self._last_normal_geometry = (
            saved_geometry or self.geometry()
        )
        self._exiting = False
        self.tray_icon = None

        self.proxy_mgr = ProxyManager()
        self.event_q = queue.Queue()
        self.engine = DownloadEngine(
            self.proxy_mgr,
            self.event_q,
            max_active_downloads=self.settings.get("queue_limit", 5),
        )

        # Default list order: active/status groups first, newest torrent
        # first inside each group. When every torrent is Finished this naturally
        # leaves the newest Finished torrent at the top.
        self.sort_key = "status"
        self.sort_reverse = False
        self.selected_uid = None
        self._restoring_queue = False
        self._queue_restore_done = False
        self._queue_restore_complete = False
        self._pending_external_sources = []
        self._row_to_uid = {}

        # Real Canvas progress bars are overlaid on the Treeview Progress
        # cells so they can use gradients and a moving gloss/shine effect.
        self._progress_canvases = {}
        self._progress_anim_phase = 0.0
        self._progress_anim_direction = 1
        self._progress_animation_job = None

        self._build_ui()
        self._setup_tray_icon()
        self.after(50, self._apply_saved_window_state)
        self.after(200, self._poll_events)
        self.after(250, self._poll_ipc_messages)
        # Restore the previous torrent queue after the widgets/proxy settings
        # have been initialized.
        self.after(900, self._restore_torrent_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Configure>", self._track_window_geometry, add="+")

    def report_callback_exception(self, exc, val, tb):
        """Keep Tk callback errors from killing/vanishing in a no-console EXE."""
        import traceback

        details = "".join(traceback.format_exception(exc, val, tb))
        log_path = log_external_error(
            "Unhandled GUI callback exception.",
            details=details,
        )

        try:
            self._append_status(
                (
                    f"ERROR: {getattr(exc, '__name__', str(exc))}: {val}. "
                    f"The application is still running."
                ),
                "ERROR",
            )
        except Exception:
            pass

        try:
            messagebox.showerror(
                "GetTorrent - error",
                (
                    "An operation failed, but GetTorrent will stay open."
                    "\n\n"
                    f"{getattr(exc, '__name__', str(exc))}: {val}"
                    + (
                        f"\n\nDiagnostic log:\n{log_path}"
                        if log_path
                        else ""
                    )
                ),
            )
        except Exception:
            pass

    @staticmethod
    def _hex_rgb(value):
        value = str(value or "#000000").lstrip("#")
        if len(value) != 6:
            return (0, 0, 0)
        try:
            return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
        except Exception:
            return (0, 0, 0)

    @classmethod
    def _mix_hex(cls, color_a, color_b, frac):
        frac = max(0.0, min(1.0, float(frac)))
        a = cls._hex_rgb(color_a)
        b = cls._hex_rgb(color_b)
        mixed = tuple(
            int(round(a[i] + (b[i] - a[i]) * frac))
            for i in range(3)
        )
        return "#%02x%02x%02x" % mixed

    def _apply_windows_titlebar_theme(self):
        """Tint native Windows title chrome when the OS supports it.

        Windows 11 supports exact caption/border/text colors. Windows 10 does
        not expose arbitrary per-window caption colors through the documented
        DWM API, so we also request its dark-title treatment as a fallback.
        """
        if sys.platform != "win32":
            return

        try:
            import ctypes

            self.update_idletasks()
            user32 = ctypes.windll.user32
            dwmapi = ctypes.windll.dwmapi

            child = int(self.winfo_id())
            hwnd = int(user32.GetParent(child) or child)
            if hwnd <= 0:
                return

            def colorref(hex_color):
                r, g, b = self._hex_rgb(hex_color)
                return ctypes.c_uint32(r | (g << 8) | (b << 16))

            # Exact title/border/text tint. These attributes are supported on
            # Windows 11; unsupported Windows versions simply reject them.
            for attribute, color in (
                (35, self.theme.get("panel_alt", "#252c33")),  # CAPTION_COLOR
                (36, self.theme.get("text", "#ffffff")),       # TEXT_COLOR
                (34, self.theme.get("accent", "#5b8fbd")),     # BORDER_COLOR
            ):
                value = colorref(color)
                try:
                    dwmapi.DwmSetWindowAttribute(
                        ctypes.c_void_p(hwnd),
                        ctypes.c_uint(attribute),
                        ctypes.byref(value),
                        ctypes.sizeof(value),
                    )
                except Exception:
                    pass

            # Windows 10-era fallback used by desktop apps to request a dark
            # non-client frame. Try both known attribute numbers harmlessly.
            dark = ctypes.c_int(1)
            for attribute in (20, 19):
                try:
                    result = dwmapi.DwmSetWindowAttribute(
                        ctypes.c_void_p(hwnd),
                        ctypes.c_uint(attribute),
                        ctypes.byref(dark),
                        ctypes.sizeof(dark),
                    )
                    if int(result) == 0:
                        break
                except Exception:
                    pass
        except Exception:
            pass

    def _clear_progress_overlays(self):
        for canvas in list(getattr(self, "_progress_canvases", {}).values()):
            try:
                canvas.destroy()
            except Exception:
                pass
        self._progress_canvases = {}

    def _progress_track_color(self, item):
        if item.state == "error":
            return self.theme.get("error", "#6a343a")
        if item.state in ("paused", "stopped", "queued"):
            return self.theme.get("panel_alt", "#343e47")
        return self.theme.get("field_alt", "#39434d")

    def _draw_progress_bar(self, canvas, item):
        try:
            width = max(20, int(canvas.winfo_width()))
            height = max(16, int(canvas.winfo_height()))
        except Exception:
            return

        canvas.delete("all")
        frac = max(0.0, min(1.0, float(item.progress or 0.0)))

        x1 = 3
        y1 = 3
        x2 = max(x1 + 2, width - 3)
        y2 = max(y1 + 2, height - 3)
        inner_w = max(1, x2 - x1)
        inner_h = max(1, y2 - y1)

        track = self._progress_track_color(item)
        border = self.theme.get("border", "#4b5864")
        canvas.configure(bg=self.theme.get("field", "#313a43"))
        canvas.create_rectangle(
            x1, y1, x2, y2,
            fill=track,
            outline=border,
            width=1,
        )

        fill_w = int(round(inner_w * frac))
        if fill_w > 0:
            accent_a = self.theme.get("accent", "#5b8fbd")
            accent_b = self.theme.get("accent2", "#65d4c8")

            # Multi-slice horizontal gradient.
            steps = max(8, min(72, fill_w // 3))
            for i in range(steps):
                sx1 = x1 + int(fill_w * i / steps)
                sx2 = x1 + int(fill_w * (i + 1) / steps) + 1
                t = i / float(max(1, steps - 1))
                base_color = self._mix_hex(accent_a, accent_b, t)
                canvas.create_rectangle(
                    sx1, y1 + 1,
                    min(x1 + fill_w, sx2), y2 - 1,
                    fill=base_color,
                    outline="",
                )

                # Glassy highlight across the upper portion of the gradient.
                gloss = self._mix_hex(base_color, "#ffffff", 0.16)
                canvas.create_rectangle(
                    sx1, y1 + 1,
                    min(x1 + fill_w, sx2),
                    y1 + max(2, int(inner_h * 0.34)),
                    fill=gloss,
                    outline="",
                )

            # Animated shine sweeps back and forth while actually downloading.
            active = (
                item.state == "downloading"
                and frac < 0.999999
            )
            if active and fill_w >= 8:
                center = x1 + int(
                    max(0, fill_w - 1)
                    * float(self._progress_anim_phase)
                )
                band = max(8, min(28, fill_w // 4))
                for offset_frac, whiten in (
                    (-1.0, 0.18),
                    (-0.55, 0.32),
                    (0.0, 0.62),
                    (0.55, 0.32),
                    (1.0, 0.18),
                ):
                    bx = int(center + offset_frac * band * 0.45)
                    bw = max(2, band // 5)
                    left = max(x1 + 1, bx - bw // 2)
                    right = min(x1 + fill_w - 1, bx + bw // 2)
                    if right <= left:
                        continue

                    local_t = max(
                        0.0,
                        min(
                            1.0,
                            (left - x1) / float(max(1, fill_w)),
                        ),
                    )
                    local = self._mix_hex(accent_a, accent_b, local_t)
                    shine = self._mix_hex(local, "#ffffff", whiten)
                    canvas.create_rectangle(
                        left, y1 + 1, right, y2 - 1,
                        fill=shine,
                        outline="",
                    )

        # Centered percent text with a tiny shadow for readability on every
        # gradient/theme.
        percent = f"{frac * 100.0:.1f}%"
        cx = width // 2
        cy = height // 2
        canvas.create_text(
            cx + 1, cy + 1,
            text=percent,
            fill="#101010",
            font=("Segoe UI", 10, "bold"),
        )
        canvas.create_text(
            cx, cy,
            text=percent,
            fill="#ffffff",
            font=("Segoe UI", 10, "bold"),
        )

    def _progress_overlay_click(self, row, event=None):
        try:
            ctrl = bool(getattr(event, "state", 0) & 0x0004)
            if ctrl:
                selected = set(self.tree.selection())
                if row in selected:
                    self.tree.selection_remove(row)
                else:
                    self.tree.selection_add(row)
            else:
                self.tree.selection_set(row)
            self.tree.focus(row)
            self._on_select_torrent()
        except Exception:
            pass
        return "break"

    def _progress_overlay_context(self, row, event):
        try:
            self.tree.selection_set(row)
            self.tree.focus(row)
            self._on_select_torrent()
            self.torrent_menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self.torrent_menu.grab_release()
            except Exception:
                pass
        return "break"

    def _layout_progress_overlays(self):
        if getattr(self, "tree", None) is None:
            return

        visible_rows = set()
        for row in self.tree.get_children():
            try:
                bbox = self.tree.bbox(row, "progress")
            except Exception:
                bbox = ()

            if not bbox:
                continue

            x, y, width, height = bbox
            if width <= 8 or height <= 8:
                continue

            uid = self._row_to_uid.get(row)
            item = self.engine.items.get(uid) if uid is not None else None
            if item is None:
                continue

            visible_rows.add(row)
            canvas = self._progress_canvases.get(row)
            if canvas is None or not canvas.winfo_exists():
                canvas = tk.Canvas(
                    self.tree,
                    highlightthickness=0,
                    bd=0,
                    takefocus=0,
                    cursor="hand2",
                )
                canvas.bind(
                    "<Button-1>",
                    lambda event, r=row: self._progress_overlay_click(r, event),
                )
                canvas.bind(
                    "<Button-3>",
                    lambda event, r=row: self._progress_overlay_context(r, event),
                )
                canvas.bind(
                    "<Double-1>",
                    lambda event, r=row: (
                        self._progress_overlay_click(r, event),
                        self._open_selected_file_location(),
                    ),
                )
                self._progress_canvases[row] = canvas

            canvas.place(
                x=x + 1,
                y=y + 1,
                width=max(1, width - 2),
                height=max(1, height - 2),
            )
            self._draw_progress_bar(canvas, item)

        for row, canvas in list(self._progress_canvases.items()):
            if row not in visible_rows:
                try:
                    canvas.place_forget()
                except Exception:
                    pass

    def _redraw_progress_overlays(self):
        for row, canvas in list(self._progress_canvases.items()):
            try:
                if not canvas.winfo_ismapped():
                    continue
            except Exception:
                continue

            uid = self._row_to_uid.get(row)
            item = self.engine.items.get(uid) if uid is not None else None
            if item is not None:
                self._draw_progress_bar(canvas, item)

    def _animate_progress_overlays(self):
        if getattr(self, "_exiting", False):
            self._progress_animation_job = None
            return

        active = False
        for row in list(getattr(self, "_progress_canvases", {})):
            uid = self._row_to_uid.get(row)
            item = self.engine.items.get(uid) if uid is not None else None
            if (
                item is not None
                and item.state == "downloading"
                and float(item.progress or 0.0) < 0.999999
            ):
                active = True
                break

        if active:
            self._progress_anim_phase += (
                0.045 * self._progress_anim_direction
            )
            if self._progress_anim_phase >= 1.0:
                self._progress_anim_phase = 1.0
                self._progress_anim_direction = -1
            elif self._progress_anim_phase <= 0.0:
                self._progress_anim_phase = 0.0
                self._progress_anim_direction = 1

            self._redraw_progress_overlays()
            delay = 55
        else:
            delay = 250

        self._progress_animation_job = self.after(
            delay,
            self._animate_progress_overlays,
        )

    def _draw_theme_gradient(self, event=None):
        canvas = getattr(self, "theme_strip", None)
        if canvas is None:
            return
        try:
            width = max(1, int(canvas.winfo_width()))
            height = max(1, int(canvas.winfo_height()))
            c1 = self.theme.get("accent", "#5b8fbd")
            c2 = self.theme.get("accent2", c1)

            def rgb(value):
                value = value.lstrip("#")
                return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))

            a = rgb(c1)
            b = rgb(c2)
            canvas.delete("theme_gradient")
            steps = min(160, max(24, width // 8))
            step_width = width / float(steps)
            for i in range(steps):
                frac = i / float(max(1, steps - 1))
                color = "#%02x%02x%02x" % tuple(
                    int(a[j] + (b[j] - a[j]) * frac)
                    for j in range(3)
                )
                x1 = int(i * step_width)
                x2 = int((i + 1) * step_width) + 1
                canvas.create_rectangle(
                    x1, 0, x2, height,
                    fill=color,
                    outline="",
                    tags="theme_gradient",
                )
        except Exception:
            pass

    def _apply_theme_colors(self):
        c = self.theme
        try:
            self.configure(background=c["bg"])
        except Exception:
            pass

        style = ttk.Style(self)
        style.configure(
            ".",
            background=c["bg"],
            foreground=c["text"],
            bordercolor=c["border"],
            darkcolor=c["border"],
            lightcolor=c["border"],
            troughcolor=c["field"],
        )
        style.configure("TFrame", background=c["bg"])
        style.configure(
            "TLabel",
            background=c["bg"],
            foreground=c["text"],
        )
        style.configure(
            "Header.TLabel",
            background=c["bg"],
            foreground=c["text"],
        )
        style.configure(
            "Small.TLabel",
            background=c["bg"],
            foreground=c["muted"],
        )
        style.configure(
            "TLabelframe",
            background=c["panel"],
            bordercolor=c["border"],
        )
        style.configure(
            "TLabelframe.Label",
            background=c["panel"],
            foreground=c["text"],
        )
        style.configure(
            "TButton",
            background=c["button"],
            foreground=c["text"],
            bordercolor=c["border"],
            focuscolor=c["accent"],
        )
        style.map(
            "TButton",
            background=[
                ("active", c["button_active"]),
                ("pressed", c["panel_alt"]),
                ("disabled", c["panel"]),
            ],
        )
        style.configure(
            "TCheckbutton",
            background=c["bg"],
            foreground=c["text"],
        )
        style.configure(
            "TEntry",
            fieldbackground=c["field"],
            background=c["field"],
            foreground=c["text"],
            insertcolor=c["text"],
            bordercolor=c["border"],
        )
        style.configure(
            "GetTorrent.TEntry",
            fieldbackground=c["field"],
            background=c["field"],
            foreground=c["text"],
            insertcolor=c["text"],
            bordercolor=c["border"],
        )
        style.map(
            "GetTorrent.TEntry",
            fieldbackground=[
                ("focus", c["field_alt"]),
                ("disabled", c["panel"]),
            ],
            foreground=[
                ("focus", c["text"]),
                ("disabled", c["muted"]),
            ],
        )
        style.configure(
            "GetTorrent.TSpinbox",
            fieldbackground=c["field"],
            background=c["button"],
            foreground=c["text"],
            insertcolor=c["text"],
            arrowcolor=c["text"],
            bordercolor=c["border"],
        )
        style.map(
            "GetTorrent.TSpinbox",
            fieldbackground=[
                ("focus", c["field_alt"]),
                ("readonly", c["field"]),
                ("disabled", c["panel"]),
            ],
            background=[
                ("active", c["button_active"]),
                ("pressed", c["panel_alt"]),
                ("disabled", c["panel"]),
            ],
            foreground=[
                ("focus", c["text"]),
                ("readonly", c["text"]),
                ("disabled", c["muted"]),
            ],
            arrowcolor=[
                ("active", c["text"]),
                ("disabled", c["muted"]),
            ],
        )
        style.configure(
            "TCombobox",
            fieldbackground=c["field"],
            background=c["button"],
            foreground=c["text"],
            arrowcolor=c["text"],
            bordercolor=c["border"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[
                ("readonly", c["field"]),
                ("disabled", c["panel"]),
            ],
            foreground=[
                ("readonly", c["text"]),
                ("disabled", c["muted"]),
            ],
            selectbackground=[("readonly", c["select"])],
            selectforeground=[("readonly", c["text"])],
        )
        style.configure(
            "TNotebook",
            background=c["bg"],
            bordercolor=c["border"],
        )
        style.configure(
            "TNotebook.Tab",
            background=c["panel"],
            foreground=c["muted"],
            bordercolor=c["border"],
        )
        style.map(
            "TNotebook.Tab",
            background=[
                ("selected", c["panel_alt"]),
                ("active", c["button_active"]),
            ],
            foreground=[
                ("selected", c["text"]),
                ("active", c["text"]),
            ],
        )
        style.configure("TPanedwindow", background=c["border"])
        style.configure(
            "TScrollbar",
            background=c["button"],
            troughcolor=c["bg"],
            arrowcolor=c["text"],
            bordercolor=c["border"],
        )
        style.configure(
            "Treeview",
            background=c["field"],
            fieldbackground=c["field"],
            foreground=c["text"],
            bordercolor=c["border"],
            lightcolor=c["border"],
            darkcolor=c["border"],
        )
        style.map(
            "Treeview",
            background=[("selected", c["select"])],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Treeview.Heading",
            background=c["panel_alt"],
            foreground=c["text"],
            bordercolor=c["border"],
        )
        style.map(
            "Treeview.Heading",
            background=[("active", c["button_active"])],
        )

        self._apply_menu_bar_colors()

        for menu in (
            getattr(self, "file_menu", None),
            getattr(self, "theme_menu", None),
            getattr(self, "about_menu", None),
            getattr(self, "torrent_menu", None),
            getattr(self, "remove_menu", None),
        ):
            if menu is None:
                continue
            try:
                menu.configure(
                    background=c["panel_alt"],
                    foreground=c["text"],
                    activebackground=c["select"],
                    activeforeground="#ffffff",
                    selectcolor=c["accent"],
                )
            except Exception:
                pass

        for widget in (
            getattr(self, "proxy_text", None),
            getattr(self, "proxy_detail_text", None),
            getattr(self, "status_text", None),
        ):
            if widget is None:
                continue
            try:
                widget.configure(
                    bg=c["field"],
                    fg=c["text"],
                    insertbackground=c["text"],
                    selectbackground=c["select"],
                    selectforeground="#ffffff",
                    highlightbackground=c["border"],
                    highlightcolor=c["accent"],
                )
            except Exception:
                pass

        if getattr(self, "file_canvas", None) is not None:
            try:
                self.file_canvas.configure(bg=c["panel"])
            except Exception:
                pass

        if getattr(self, "proxy_hint", None) is not None:
            try:
                self.proxy_hint.configure(foreground=c["muted"])
            except Exception:
                pass

        if getattr(self, "tree", None) is not None:
            try:
                self.tree.tag_configure(
                    "metadata",
                    background=c["metadata"],
                    foreground=c["text"],
                )
                self.tree.tag_configure(
                    "queued",
                    background=c["field"],
                    foreground=c["muted"],
                )
                self.tree.tag_configure(
                    "stopped",
                    background=c["panel_alt"],
                    foreground=c["muted"],
                )
                self.tree.tag_configure(
                    "paused",
                    background=c["field_alt"],
                    foreground=c["muted"],
                )
                self.tree.tag_configure(
                    "error",
                    background=c["error"],
                    foreground="#ffffff",
                )
            except Exception:
                pass

        if getattr(self, "status_text", None) is not None:
            try:
                self.status_text.tag_configure("ERROR", foreground="#ff7878")
                self.status_text.tag_configure("WARN", foreground="#ffd166")
                self.status_text.tag_configure(
                    "TORRENT",
                    foreground=c["accent2"],
                )
                self.status_text.tag_configure("OK", foreground=c["green"])
                self.status_text.tag_configure("INFO", foreground=c["text"])
            except Exception:
                pass

        self._draw_theme_gradient()

    def _set_theme(self, name):
        if name not in self.THEMES:
            return
        self.theme_name = name
        self.theme = dict(self.THEMES[name])
        if getattr(self, "theme_var", None) is not None:
            self.theme_var.set(name)
        self._apply_theme_colors()
        self.after_idle(self._apply_windows_titlebar_theme)
        self.after_idle(self._layout_progress_overlays)
        self._schedule_input_save()
        try:
            self._append_status(f"🎨 Theme changed to {name}.", "INFO")
        except Exception:
            pass

    def _column_min_width(self, key):
        return max(
            24,
            int(self.COLUMN_MIN_WIDTHS.get(key, 60) * self.scale),
        )

    def _begin_column_resize(self, event):
        if getattr(self, "tree", None) is None:
            return
        col_id = self.tree.identify_column(event.x)
        if not col_id.startswith("#"):
            return
        try:
            index = int(col_id[1:]) - 1
            key = self.COLUMNS[index][0]
        except Exception:
            return

        widths = {
            col_key: int(self.tree.column(col_key, "width"))
            for col_key, _label, _width in self.COLUMNS
        }
        self._column_resize_state = {
            "key": key,
            "start_x": int(event.x),
            "widths": widths,
        }

    def _shrink_columns_equally(self, widths, keys, amount):
        remaining = max(0, int(amount))
        active = list(keys)
        while remaining > 0 and active:
            share = max(1, remaining // len(active))
            next_active = []
            changed = False
            for key in active:
                if remaining <= 0:
                    break
                capacity = max(
                    0,
                    int(widths[key]) - self._column_min_width(key),
                )
                if capacity <= 0:
                    continue
                take = min(capacity, share, remaining)
                if take > 0:
                    widths[key] -= take
                    remaining -= take
                    changed = True
                if widths[key] > self._column_min_width(key):
                    next_active.append(key)
            if not changed:
                break
            active = next_active
        return remaining

    def _grow_columns_equally(self, widths, keys, amount):
        remaining = max(0, int(amount))
        keys = list(keys)
        if not keys:
            return
        while remaining > 0:
            share = max(1, remaining // len(keys))
            for key in keys:
                if remaining <= 0:
                    break
                add = min(share, remaining)
                widths[key] += add
                remaining -= add

    def _resize_column_motion(self, event):
        state = getattr(self, "_column_resize_state", None)
        if not state or getattr(self, "tree", None) is None:
            return None

        key = state["key"]
        start_widths = state["widths"]
        start_width = int(start_widths[key])
        requested_delta = int(event.x) - int(state["start_x"])

        widths = dict(start_widths)
        others = [k for k, _label, _width in self.COLUMNS if k != key]
        target_min = self._column_min_width(key)

        if requested_delta >= 0:
            remaining = self._shrink_columns_equally(
                widths,
                others,
                requested_delta,
            )
            actual_growth = requested_delta - remaining
            widths[key] = start_width + actual_growth
        else:
            requested_shrink = min(
                -requested_delta,
                max(0, start_width - target_min),
            )
            widths[key] = start_width - requested_shrink
            self._grow_columns_equally(
                widths,
                others,
                requested_shrink,
            )

        for col_key, _label, _default in self.COLUMNS:
            self.tree.column(
                col_key,
                width=max(
                    self._column_min_width(col_key),
                    int(widths[col_key]),
                ),
            )
        self.after_idle(self._layout_progress_overlays)
        return "break"

    def _end_column_resize(self, event=None):
        if getattr(self, "_column_resize_state", None) is not None:
            if event is not None:
                self._resize_column_motion(event)
            self._column_resize_state = None
            self.settings["column_widths"] = self._current_column_widths()
            self._persist_inputs()
            return "break"
        return None

    def _post_app_menu(self, button, menu):
        """Post a themed popup menu directly beneath its toolbar button."""
        try:
            self.update_idletasks()
            x = int(button.winfo_rootx())
            y = int(button.winfo_rooty() + button.winfo_height())
            menu.tk_popup(x, y)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def _queue_limit_step(self, delta):
        try:
            value = int(self.queue_limit_var.get())
        except Exception:
            value = 5
        value = max(1, min(100, value + int(delta)))
        self.queue_limit_var.set(value)
        self._on_queue_limit_changed()

    def _apply_menu_bar_colors(self):
        c = self.theme
        try:
            self.app_menu_frame.configure(
                bg=c["panel_alt"],
                highlightbackground=c["border"],
                highlightcolor=c["border"],
            )
        except Exception:
            pass

        for button in (
            getattr(self, "file_menu_button", None),
            getattr(self, "about_menu_button", None),
        ):
            if button is None:
                continue
            try:
                button.configure(
                    bg=c["panel_alt"],
                    fg=c["text"],
                    activebackground=c["select"],
                    activeforeground="#ffffff",
                    highlightbackground=c["panel_alt"],
                    highlightcolor=c["accent"],
                    selectcolor=c["accent"],
                )
            except Exception:
                pass

        for frame_name in (
            "queue_limit_frame",
            "queue_limit_arrows",
        ):
            frame = getattr(self, frame_name, None)
            if frame is not None:
                try:
                    frame.configure(
                        bg=c["border"],
                        highlightbackground=c["border"],
                        highlightcolor=c["accent"],
                    )
                except Exception:
                    pass

        for button_name in (
            "queue_limit_up_button",
            "queue_limit_down_button",
        ):
            button = getattr(self, button_name, None)
            if button is not None:
                try:
                    button.configure(
                        bg=c["button"],
                        fg=c["text"],
                        activebackground=c["button_active"],
                        activeforeground="#ffffff",
                        highlightbackground=c["button"],
                    )
                except Exception:
                    pass

    def _current_column_widths(self):
        if getattr(self, "tree", None) is None:
            return {}
        widths = {}
        for key, _label, _default in self.COLUMNS:
            try:
                widths[key] = int(self.tree.column(key, "width"))
            except Exception:
                pass
        return widths

    def _reset_column_sizes(self):
        if getattr(self, "tree", None) is None:
            return

        for key, _label, default_width in self.COLUMNS:
            self.tree.column(
                key,
                width=max(
                    self._column_min_width(key),
                    int(default_width * self.scale),
                ),
            )

        self.settings["column_widths"] = self._current_column_widths()
        self.after_idle(self._layout_progress_overlays)
        self._persist_inputs()
        try:
            self._append_status(
                "↔ Column sizes reset to defaults.",
                "INFO",
            )
        except Exception:
            pass

    def _build_ui(self):
        pad = 6
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # Selected palette. Each theme uses layered tones plus a two-color
        # accent gradient strip for a distinct visual identity.
        self.theme = dict(self.THEMES[self.theme_name])

        c = self.theme
        self.configure(background=c["bg"])

        # Fully themed in-app menu strip. Tk's native Windows menubar is
        # intentionally not used because Windows paints it gray independently
        # of the application's selected theme.
        self.app_menu_frame = tk.Frame(
            self,
            bg=c["panel_alt"],
            height=max(28, int(30 * self.scale)),
            bd=0,
            highlightthickness=1,
            highlightbackground=c["border"],
        )
        self.app_menu_frame.pack(fill="x", side="top")
        self.app_menu_frame.pack_propagate(False)

        self.file_menu = tk.Menu(
            self,
            tearoff=0,
            background=c["panel_alt"],
            foreground=c["text"],
            activebackground=c["select"],
            activeforeground="#ffffff",
            selectcolor=c["accent"],
        )
        self.file_menu.add_command(
            label="▣ Show App",
            command=self._show_app,
        )
        self.file_menu.add_command(
            label="📌 Minimize to System Tray",
            command=self._hide_to_tray,
        )

        self.theme_var = tk.StringVar(value=self.theme_name)
        self.theme_menu = tk.Menu(
            self.file_menu,
            tearoff=0,
            background=c["panel_alt"],
            foreground=c["text"],
            activebackground=c["select"],
            activeforeground="#ffffff",
            selectcolor=c["accent"],
        )
        for theme_name in self.THEMES:
            self.theme_menu.add_radiobutton(
                label=theme_name,
                value=theme_name,
                variable=self.theme_var,
                command=lambda n=theme_name: self._set_theme(n),
            )
        self.file_menu.add_cascade(
            label="🎨 Theme",
            menu=self.theme_menu,
        )
        self.file_menu.add_separator()
        self.file_menu.add_command(
            label="↔ Reset Column Sizes",
            command=self._reset_column_sizes,
        )
        self.file_menu.add_separator()
        self.file_menu.add_command(
            label="🚪 Exit App",
            command=self._exit_app,
        )

        self.file_menu_button = tk.Button(
            self.app_menu_frame,
            text="File",
            command=lambda: self._post_app_menu(
                self.file_menu_button,
                self.file_menu,
            ),
            bg=c["panel_alt"],
            fg=c["text"],
            activebackground=c["select"],
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=max(8, int(10 * self.scale)),
            cursor="hand2",
            font=("Segoe UI", 12),
            takefocus=0,
        )
        self.file_menu_button.pack(side="left", fill="y")

        self.about_menu = tk.Menu(
            self,
            tearoff=0,
            background=c["panel_alt"],
            foreground=c["text"],
            activebackground=c["select"],
            activeforeground="#ffffff",
        )
        self.about_menu.add_command(
            label="ℹ About GetTorrent",
            command=self._show_about_dialog,
        )
        self.about_menu.add_separator()
        self.about_menu.add_command(
            label="☕ Buy Me a Coffee",
            command=self._open_buy_me_a_coffee,
        )

        self.about_menu_button = tk.Button(
            self.app_menu_frame,
            text="About",
            command=lambda: self._post_app_menu(
                self.about_menu_button,
                self.about_menu,
            ),
            bg=c["panel_alt"],
            fg=c["text"],
            activebackground=c["select"],
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=max(8, int(10 * self.scale)),
            cursor="hand2",
            font=("Segoe UI", 12),
            takefocus=0,
        )
        self.about_menu_button.pack(side="left", fill="y")

        style.configure(
            ".",
            background=c["bg"],
            foreground=c["text"],
            bordercolor=c["border"],
            darkcolor=c["border"],
            lightcolor=c["border"],
            troughcolor=c["field"],
        )
        style.configure("TFrame", background=c["bg"])
        style.configure(
            "TLabel",
            font=self.font_normal,
            background=c["bg"],
            foreground=c["text"],
        )
        style.configure(
            "Header.TLabel",
            font=self.font_bold,
            background=c["bg"],
            foreground=c["text"],
        )
        style.configure(
            "Small.TLabel",
            font=self.font_small,
            background=c["bg"],
            foreground=c["muted"],
        )
        style.configure(
            "TLabelframe",
            background=c["panel"],
            bordercolor=c["border"],
            relief="solid",
        )
        style.configure(
            "TLabelframe.Label",
            font=self.font_bold,
            background=c["panel"],
            foreground=c["text"],
        )
        style.configure(
            "TButton",
            font=self.font_normal,
            padding=(8, 5),
            background=c["button"],
            foreground=c["text"],
            bordercolor=c["border"],
            focuscolor=c["accent"],
        )
        style.map(
            "TButton",
            background=[
                ("active", c["button_active"]),
                ("pressed", c["panel_alt"]),
                ("disabled", c["panel"]),
            ],
            foreground=[("disabled", "#7f8992")],
        )
        style.configure(
            "TCheckbutton",
            font=self.font_normal,
            background=c["bg"],
            foreground=c["text"],
        )
        style.configure(
            "TEntry",
            font=self.font_normal,
            fieldbackground=c["field"],
            background=c["field"],
            foreground=c["text"],
            insertcolor=c["text"],
            bordercolor=c["border"],
            lightcolor=c["border"],
            darkcolor=c["border"],
        )
        style.map(
            "TEntry",
            fieldbackground=[
                ("focus", c["field_alt"]),
                ("disabled", c["panel"]),
            ],
            foreground=[
                ("focus", c["text"]),
                ("disabled", c["muted"]),
            ],
            insertcolor=[("focus", c["text"])],
        )
        style.configure(
            "GetTorrent.TEntry",
            font=self.font_normal,
            fieldbackground=c["field"],
            background=c["field"],
            foreground=c["text"],
            insertcolor=c["text"],
            bordercolor=c["border"],
            lightcolor=c["border"],
            darkcolor=c["border"],
            padding=(8, 6),
        )
        style.map(
            "GetTorrent.TEntry",
            fieldbackground=[
                ("focus", c["field_alt"]),
                ("disabled", c["panel"]),
            ],
            foreground=[
                ("focus", c["text"]),
                ("disabled", c["muted"]),
            ],
            insertcolor=[("focus", c["text"])],
        )
        style.configure(
            "GetTorrent.TSpinbox",
            font=self.font_normal,
            fieldbackground=c["field"],
            background=c["button"],
            foreground=c["text"],
            insertcolor=c["text"],
            arrowcolor=c["text"],
            bordercolor=c["border"],
            lightcolor=c["border"],
            darkcolor=c["border"],
            padding=(4, 3),
        )
        style.map(
            "GetTorrent.TSpinbox",
            fieldbackground=[
                ("focus", c["field_alt"]),
                ("readonly", c["field"]),
                ("disabled", c["panel"]),
            ],
            background=[
                ("active", c["button_active"]),
                ("pressed", c["panel_alt"]),
                ("disabled", c["panel"]),
            ],
            foreground=[
                ("focus", c["text"]),
                ("readonly", c["text"]),
                ("disabled", c["muted"]),
            ],
            arrowcolor=[
                ("active", c["text"]),
                ("disabled", c["muted"]),
            ],
        )
        style.configure(
            "TCombobox",
            font=self.font_normal,
            fieldbackground=c["field"],
            background=c["button"],
            foreground=c["text"],
            arrowcolor=c["text"],
            bordercolor=c["border"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[
                ("readonly", c["field"]),
                ("disabled", c["panel"]),
            ],
            foreground=[
                ("readonly", c["text"]),
                ("disabled", "#7f8992"),
            ],
            selectbackground=[("readonly", c["select"])],
            selectforeground=[("readonly", c["text"])],
        )
        style.configure(
            "TNotebook",
            background=c["bg"],
            bordercolor=c["border"],
        )
        style.configure(
            "TNotebook.Tab",
            font=self.font_bold,
            padding=(14, 8),
            background=c["panel"],
            foreground=c["muted"],
            bordercolor=c["border"],
        )
        style.map(
            "TNotebook.Tab",
            background=[
                ("selected", c["panel_alt"]),
                ("active", c["button_active"]),
            ],
            foreground=[
                ("selected", c["text"]),
                ("active", c["text"]),
            ],
        )
        style.configure(
            "TPanedwindow",
            background=c["border"],
        )
        style.configure(
            "TScrollbar",
            background=c["button"],
            troughcolor=c["bg"],
            arrowcolor=c["text"],
            bordercolor=c["border"],
        )

        tree_font = tkfont.Font(font=self.font_normal)
        row_height = max(36, tree_font.metrics("linespace") + 9)
        style.configure(
            "Treeview",
            font=self.font_normal,
            rowheight=row_height,
            background=c["field"],
            fieldbackground=c["field"],
            foreground=c["text"],
            bordercolor=c["border"],
            lightcolor=c["border"],
            darkcolor=c["border"],
        )
        style.map(
            "Treeview",
            background=[("selected", c["select"])],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Treeview.Heading",
            font=self.font_bold,
            background=c["panel_alt"],
            foreground=c["text"],
            bordercolor=c["border"],
            relief="flat",
        )
        style.map(
            "Treeview.Heading",
            background=[("active", c["button_active"])],
        )

        # A slim accent gradient gives every palette a distinct themed look
        # without consuming useful application space.
        self.theme_strip = tk.Canvas(
            self,
            height=max(5, int(6 * self.scale)),
            highlightthickness=0,
            bd=0,
            bg=c["accent"],
        )
        self.theme_strip.pack(fill="x", side="top")
        self.theme_strip.bind(
            "<Configure>",
            self._draw_theme_gradient,
        )
        self.after_idle(self._draw_theme_gradient)

        # Root uses grid so App Status always owns the bottom row and can never
        # be pushed out of view by a tab's expandable content.
        root = ttk.Frame(self, padding=pad)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        root.rowconfigure(1, weight=0)

        # ===================================================================
        # MAIN TABS: Downloads | Add Torrent
        # ===================================================================
        self.main_notebook = ttk.Notebook(root)
        self.main_notebook.grid(
            row=0,
            column=0,
            sticky="nsew",
            pady=(0, pad),
        )

        downloads_tab = ttk.Frame(self.main_notebook, padding=pad)
        proxies_tab = ttk.Frame(self.main_notebook, padding=pad)
        add_tab = ttk.Frame(self.main_notebook, padding=pad)

        # Direct-only application: proxy controls were removed from the
        # user-facing application because public proxy routing was unreliable.
        self.main_notebook.add(downloads_tab, text="📥 Downloads")
        self.main_notebook.add(add_tab, text="➕ Add Torrent")

        self.main_notebook.bind(
            "<<NotebookTabChanged>>",
            self._schedule_input_save,
        )

        # ===================================================================
        # DOWNLOADS TAB
        # Torrent list + Files in selected torrent stay together here.
        # A vertical PanedWindow lets the user decide how much space each gets.
        # ===================================================================
        downloads_tab.columnconfigure(0, weight=1)
        downloads_tab.rowconfigure(1, weight=1)

        tbtns = ttk.Frame(downloads_tab)
        tbtns.grid(row=0, column=0, sticky="ew", pady=(0, pad))

        ttk.Button(
            tbtns,
            text="🗑 Remove selected",
            command=lambda: self._remove_selected(
                delete_data=False,
                confirm=False,
            ),
        ).pack(side="left")

        ttk.Button(
            tbtns,
            text="▶ Start / resume all",
            command=self._start_all,
        ).pack(side="left", padx=(pad, 0))

        ttk.Button(
            tbtns,
            text="⏸ Pause all",
            command=self._stop_all,
        ).pack(side="left", padx=(pad, 0))

        ttk.Label(
            tbtns,
            text="Concurrent downloads:",
            style="Small.TLabel",
        ).pack(side="left", padx=(pad * 2, 4))

        self.queue_limit_var = tk.IntVar(
            value=int(self.settings.get("queue_limit", 5))
        )

        # Custom spinner: a themed numeric entry with large, full-height
        # arrow buttons. This avoids Tk's tiny native spinbox arrows on
        # high-DPI Windows displays.
        spinner_height = max(36, int(38 * self.scale))
        spinner_entry_width = max(54, int(60 * self.scale))
        spinner_arrow_width = max(30, int(34 * self.scale))

        self.queue_limit_frame = tk.Frame(
            tbtns,
            bg=c["border"],
            bd=0,
            highlightthickness=1,
            highlightbackground=c["border"],
            width=spinner_entry_width + spinner_arrow_width,
            height=spinner_height,
        )
        self.queue_limit_frame.pack(side="left")
        self.queue_limit_frame.pack_propagate(False)
        self.queue_limit_frame.grid_propagate(False)

        self.queue_limit_frame.grid_rowconfigure(0, weight=1)
        self.queue_limit_frame.grid_columnconfigure(
            0,
            minsize=spinner_entry_width,
            weight=1,
        )
        self.queue_limit_frame.grid_columnconfigure(
            1,
            minsize=spinner_arrow_width,
            weight=0,
        )

        self.queue_limit_entry = ttk.Entry(
            self.queue_limit_frame,
            justify="center",
            textvariable=self.queue_limit_var,
            font=self.font_normal,
            style="GetTorrent.TEntry",
        )
        self.queue_limit_entry.grid(
            row=0,
            column=0,
            sticky="nsew",
        )

        self.queue_limit_arrows = tk.Frame(
            self.queue_limit_frame,
            bg=c["border"],
            bd=0,
            width=spinner_arrow_width,
            height=spinner_height,
        )
        self.queue_limit_arrows.grid(
            row=0,
            column=1,
            sticky="nsew",
        )
        self.queue_limit_arrows.grid_propagate(False)
        self.queue_limit_arrows.grid_rowconfigure(0, weight=1)
        self.queue_limit_arrows.grid_rowconfigure(1, weight=1)
        self.queue_limit_arrows.grid_columnconfigure(0, weight=1)

        self.queue_limit_up_button = tk.Button(
            self.queue_limit_arrows,
            text="▲",
            command=lambda: self._queue_limit_step(1),
            bg=c["button"],
            fg=c["text"],
            activebackground=c["button_active"],
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=0,
            pady=0,
            font=("Segoe UI Symbol", 10, "bold"),
            takefocus=0,
            cursor="hand2",
        )
        self.queue_limit_up_button.grid(
            row=0,
            column=0,
            sticky="nsew",
        )

        self.queue_limit_down_button = tk.Button(
            self.queue_limit_arrows,
            text="▼",
            command=lambda: self._queue_limit_step(-1),
            bg=c["button"],
            fg=c["text"],
            activebackground=c["button_active"],
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=0,
            pady=0,
            font=("Segoe UI Symbol", 10, "bold"),
            takefocus=0,
            cursor="hand2",
        )
        self.queue_limit_down_button.grid(
            row=1,
            column=0,
            sticky="nsew",
        )

        # Compatibility alias for any older code that refers to the control
        # by its former Spinbox attribute name.
        self.queue_limit_spin = self.queue_limit_entry

        self.queue_limit_entry.bind(
            "<Return>", self._on_queue_limit_changed
        )
        self.queue_limit_entry.bind(
            "<FocusOut>", self._on_queue_limit_changed
        )
        self.queue_limit_entry.bind(
            "<Up>",
            lambda _event: (
                self._queue_limit_step(1),
                "break",
            )[1],
        )
        self.queue_limit_entry.bind(
            "<Down>",
            lambda _event: (
                self._queue_limit_step(-1),
                "break",
            )[1],
        )

        self.summary_var = tk.StringVar(value="No torrents.")
        ttk.Label(
            tbtns,
            textvariable=self.summary_var,
        ).pack(side="right")

        downloads_panes = ttk.Panedwindow(
            downloads_tab,
            orient="vertical",
        )
        downloads_panes.grid(
            row=1,
            column=0,
            sticky="nsew",
        )

        # Torrent list pane.
        tframe = ttk.LabelFrame(
            downloads_panes,
            text="Downloads",
            padding=pad,
        )
        downloads_panes.add(tframe, weight=3)

        cols = [c[0] for c in self.COLUMNS]
        self.tree = ttk.Treeview(
            tframe,
            columns=cols,
            show="headings",
            selectmode="extended",
            height=10,
        )

        centered_columns = {
            "idx", "size", "progress", "speed", "eta", "peers", "status"
        }
        saved_column_widths = self.settings.get("column_widths", {})
        if not isinstance(saved_column_widths, dict):
            saved_column_widths = {}

        for key, label, width in self.COLUMNS:
            anchor = "center" if key in centered_columns else "w"
            self.tree.heading(
                key,
                text=label,
                anchor=anchor,
                command=lambda k=key: self._sort_by(k),
            )

            default_px = int(width * self.scale)
            try:
                saved_px = int(saved_column_widths.get(key, default_px))
            except Exception:
                saved_px = default_px

            self.tree.column(
                key,
                width=max(
                    self._column_min_width(key),
                    saved_px,
                ),
                minwidth=self._column_min_width(key),
                anchor=anchor,
            )

        self.tree.pack(side="left", fill="both", expand=True)

        def tree_yview(*args):
            self.tree.yview(*args)
            self.after_idle(self._layout_progress_overlays)

        def tree_scrollbar_set(first, last):
            tsb.set(first, last)
            self.after_idle(self._layout_progress_overlays)

        tsb = ttk.Scrollbar(
            tframe,
            orient="vertical",
            command=tree_yview,
        )
        tsb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=tree_scrollbar_set)
        self.tree.bind(
            "<MouseWheel>",
            lambda _event: self.after_idle(
                self._layout_progress_overlays
            ),
            add="+",
        )
        self.tree.bind(
            "<Configure>",
            lambda _event: self.after_idle(
                self._layout_progress_overlays
            ),
            add="+",
        )

        self.tree.bind(
            "<<TreeviewSelect>>",
            self._on_select_torrent,
        )
        self.tree.bind(
            "<Button-1>",
            self._on_torrent_list_click,
        )
        self.tree.bind(
            "<B1-Motion>",
            self._resize_column_motion,
        )
        self.tree.bind(
            "<ButtonRelease-1>",
            self._end_column_resize,
        )
        self.tree.bind(
            "<Button-3>",
            self._show_torrent_context_menu,
        )
        self.tree.bind(
            "<Delete>",
            self._delete_key_remove,
        )
        self.tree.bind(
            "<Double-1>",
            self._double_click_torrent_location,
        )

        self.remove_menu = tk.Menu(self, tearoff=0)
        self.remove_menu.add_command(
            label="Torrent",
            command=lambda: self._remove_selected(
                delete_data=False,
                confirm=False,
            ),
        )
        self.remove_menu.add_command(
            label="Torrent + file",
            command=lambda: self._remove_selected(
                delete_data=True,
                confirm=True,
            ),
        )

        self.torrent_menu = tk.Menu(self, tearoff=0)
        self.torrent_menu.add_command(
            label="Play",
            command=self._play_selected,
        )
        self.torrent_menu.add_command(
            label="Pause",
            command=self._pause_selected,
        )
        self.torrent_menu.add_command(
            label="Retry",
            command=self._retry_selected_failed,
        )
        self.torrent_menu.add_command(
            label="Open file location",
            command=self._open_selected_file_location,
        )
        self.torrent_menu.add_separator()
        self.torrent_menu.add_cascade(
            label="Remove",
            menu=self.remove_menu,
        )

        for menu in (self.torrent_menu, self.remove_menu):
            try:
                menu.configure(
                    background=self.theme["panel_alt"],
                    foreground=self.theme["text"],
                    activebackground=self.theme["select"],
                    activeforeground="#ffffff",
                    selectcolor=self.theme["green"],
                    borderwidth=1,
                    relief="solid",
                )
            except Exception:
                pass

        # Normal and finished torrents are intentionally plain rows. Progress
        # color lives only in the Progress cell. Exceptional states get subtle
        # dark tints so they remain readable without flooding the whole row.
        self.tree.tag_configure(
            "metadata",
            background=self.theme["metadata"],
            foreground=self.theme["text"],
        )
        self.tree.tag_configure(
            "queued",
            background=self.theme["field"],
            foreground=self.theme["muted"],
        )
        self.tree.tag_configure(
            "stopped",
            background="#30373e",
            foreground=self.theme["muted"],
        )
        self.tree.tag_configure(
            "paused",
            background="#333b43",
            foreground=self.theme["muted"],
        )
        self.tree.tag_configure(
            "error",
            background=self.theme["error"],
            foreground="#ffffff",
        )

        # Files pane remains with Downloads as requested.
        fframe = ttk.LabelFrame(
            downloads_panes,
            text="Files in selected torrent (green = downloaded pieces)",
            padding=pad,
        )
        downloads_panes.add(fframe, weight=2)

        self.file_canvas = tk.Canvas(
            fframe,
            highlightthickness=0,
            bg=self.theme["panel"],
            bd=0,
        )
        self.file_canvas.pack(
            side="left",
            fill="both",
            expand=True,
        )

        fsb = ttk.Scrollbar(
            fframe,
            orient="vertical",
            command=self.file_canvas.yview,
        )
        fsb.pack(side="right", fill="y")
        self.file_canvas.configure(yscrollcommand=fsb.set)

        self.file_inner = ttk.Frame(self.file_canvas)
        self.file_window = self.file_canvas.create_window(
            (0, 0),
            window=self.file_inner,
            anchor="nw",
        )

        self.file_inner.bind(
            "<Configure>",
            lambda e: self.file_canvas.configure(
                scrollregion=self.file_canvas.bbox("all")
            ),
        )
        self.file_canvas.bind(
            "<Configure>",
            lambda e: self.file_canvas.itemconfig(
                self.file_window,
                width=e.width,
            ),
        )
        self._file_rows = []

        # ===================================================================
        # PROXIES TAB
        # Entire available workspace is dedicated to proxy configuration/status.
        # ===================================================================
        proxies_tab.columnconfigure(0, weight=1)
        proxies_tab.rowconfigure(1, weight=1)

        proxy_header = ttk.Frame(proxies_tab)
        proxy_header.grid(
            row=0,
            column=0,
            sticky="ew",
            pady=(0, pad),
        )
        proxy_header.columnconfigure(0, weight=1)

        ttk.Label(
            proxy_header,
            text="Route downloads through proxies",
            style="Header.TLabel",
        ).grid(row=0, column=0, sticky="w")

        self.use_proxy_var = tk.BooleanVar(value=False)
        self.proxy_switch = ToggleSwitch(
            proxy_header,
            self.use_proxy_var,
            command=self._on_proxy_toggle,
            scale=max(1.0, min(self.dpi_scale, 1.75)),
        )
        self.proxy_switch.grid(
            row=0,
            column=1,
            sticky="e",
            padx=(pad, 0),
        )

        self.proxy_hint = ttk.Label(
            proxy_header,
            text=(
                "Proxy routing enabled"
                if self.use_proxy_var.get()
                else "Direct connection"
            ),
            foreground="#777",
            style="Small.TLabel",
        )
        self.proxy_hint.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(2, 0),
        )

        # Keep Proxy List / Proxy Status as sub-tabs inside the main Proxies tab.
        self.proxy_notebook = ttk.Notebook(proxies_tab)
        self.proxy_notebook.grid(
            row=1,
            column=0,
            sticky="nsew",
        )
        self.proxy_notebook.bind(
            "<<NotebookTabChanged>>",
            self._schedule_input_save,
        )

        # ---------------- Proxy List sub-tab ----------------
        proxy_list_tab = ttk.Frame(
            self.proxy_notebook,
            padding=pad,
        )
        self.proxy_notebook.add(
            proxy_list_tab,
            text="Proxy List",
        )
        proxy_list_tab.columnconfigure(0, weight=1)
        proxy_list_tab.rowconfigure(1, weight=1)

        proxy_list_header = ttk.Frame(proxy_list_tab)
        proxy_list_header.grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="ew",
        )
        proxy_list_header.columnconfigure(0, weight=1)

        ttk.Label(
            proxy_list_header,
            text="One ip:port per line",
            style="Header.TLabel",
        ).grid(row=0, column=0, sticky="w")

        ttk.Label(
            proxy_list_header,
            text="Mode:",
            style="Small.TLabel",
        ).grid(
            row=0,
            column=1,
            sticky="e",
            padx=(pad, 3),
        )

        self.proxy_type_var = tk.StringVar(
            value=self.settings.get("proxy_type", "Auto")
        )
        self.proxy_type_combo = ttk.Combobox(
            proxy_list_header,
            textvariable=self.proxy_type_var,
            values=("Auto", "HTTP", "SOCKS5"),
            state="readonly",
            width=10,
            font=self.font_normal,
        )
        self.proxy_type_combo.grid(
            row=0,
            column=2,
            sticky="e",
        )
        self.proxy_type_combo.bind(
            "<<ComboboxSelected>>",
            self._on_proxy_type_changed,
        )

        proxy_text_frame = ttk.Frame(proxy_list_tab)
        proxy_text_frame.grid(
            row=1,
            column=0,
            sticky="nsew",
            pady=(pad // 2, 0),
        )
        proxy_text_frame.columnconfigure(0, weight=1)
        proxy_text_frame.rowconfigure(0, weight=1)

        self.proxy_text = tk.Text(
            proxy_text_frame,
            font=self.font_mono,
            wrap="none",
            bg=self.theme["field"],
            fg=self.theme["text"],
            insertbackground=self.theme["text"],
            selectbackground=self.theme["select"],
            selectforeground="#ffffff",
            highlightbackground=self.theme["border"],
            highlightcolor=self.theme["accent"],
            relief="flat",
            borderwidth=1,
        )
        self.proxy_text.grid(
            row=0,
            column=0,
            sticky="nsew",
        )

        proxy_text_vscroll = ttk.Scrollbar(
            proxy_text_frame,
            orient="vertical",
            command=self.proxy_text.yview,
        )
        proxy_text_vscroll.grid(
            row=0,
            column=1,
            sticky="ns",
        )

        proxy_text_hscroll = ttk.Scrollbar(
            proxy_text_frame,
            orient="horizontal",
            command=self.proxy_text.xview,
        )
        proxy_text_hscroll.grid(
            row=1,
            column=0,
            sticky="ew",
        )

        self.proxy_text.configure(
            yscrollcommand=proxy_text_vscroll.set,
            xscrollcommand=proxy_text_hscroll.set,
        )

        self.proxy_text.insert(
            "1.0",
            self.settings.get("proxies", ""),
        )
        self.proxy_text.bind(
            "<KeyRelease>",
            self._schedule_input_save,
        )
        self.proxy_text.bind(
            "<FocusOut>",
            self._schedule_input_save,
        )

        pbtns = ttk.Frame(proxy_list_tab)
        pbtns.grid(
            row=1,
            column=1,
            sticky="n",
            padx=(pad, 0),
            pady=(pad // 2, 0),
        )

        self.proxy_load_btn = ttk.Button(
            pbtns,
            text="Load list",
            command=self._load_proxies,
        )
        self.proxy_load_btn.pack(
            fill="x",
            pady=(0, 5),
        )

        self.proxy_file_btn = ttk.Button(
            pbtns,
            text="From file",
            command=self._load_proxies_file,
        )
        self.proxy_file_btn.pack(
            fill="x",
            pady=(0, 5),
        )

        self.proxy_test_btn = ttk.Button(
            pbtns,
            text="Test for torrent",
            command=self._test_proxies,
        )
        self.proxy_test_btn.pack(
            fill="x",
            pady=(0, 5),
        )

        self.proxy_save_btn = ttk.Button(
            pbtns,
            text="Save list",
            command=self._save_proxies_file,
        )
        self.proxy_save_btn.pack(fill="x")

        # ---------------- Proxy Status sub-tab ----------------
        proxy_status_tab = ttk.Frame(
            self.proxy_notebook,
            padding=pad,
        )
        self.proxy_notebook.add(
            proxy_status_tab,
            text="Proxy Status",
        )

        proxy_status_tab.columnconfigure(0, weight=1)
        proxy_status_tab.rowconfigure(2, weight=3)
        proxy_status_tab.rowconfigure(4, weight=2)

        proxy_state_row = ttk.Frame(proxy_status_tab)
        proxy_state_row.grid(
            row=0,
            column=0,
            sticky="ew",
        )
        proxy_state_row.columnconfigure(1, weight=1)

        self.proxy_light = tk.Label(
            proxy_state_row,
            text="●",
            foreground="#888888",
            background=self.cget("bg"),
            font=("Segoe UI", 14, "bold"),
        )
        self.proxy_light.grid(
            row=0,
            column=0,
            sticky="w",
        )

        self.proxy_connection_var = tk.StringVar(
            value="Proxy OFF"
        )
        ttk.Label(
            proxy_state_row,
            textvariable=self.proxy_connection_var,
            style="Header.TLabel",
        ).grid(
            row=0,
            column=1,
            sticky="w",
            padx=(4, 0),
        )

        self.current_proxy_var = tk.StringVar(
            value="Current proxy: none"
        )
        ttk.Label(
            proxy_status_tab,
            textvariable=self.current_proxy_var,
            style="Small.TLabel",
            anchor="w",
            justify="left",
        ).grid(
            row=1,
            column=0,
            sticky="ew",
            pady=(4, pad),
        )

        proxy_table_frame = ttk.Frame(proxy_status_tab)
        proxy_table_frame.grid(
            row=2,
            column=0,
            sticky="nsew",
        )
        proxy_table_frame.columnconfigure(0, weight=1)
        proxy_table_frame.rowconfigure(0, weight=1)

        pcols = ("proxy", "type", "status", "active")
        self.ptree = ttk.Treeview(
            proxy_table_frame,
            columns=pcols,
            show="headings",
            height=8,
        )

        self.ptree.heading(
            "proxy",
            text="Proxy (ip:port)",
        )
        self.ptree.heading(
            "type",
            text="Type",
        )
        self.ptree.heading(
            "status",
            text="Status",
        )
        self.ptree.heading(
            "active",
            text="In use",
        )

        self.ptree.column(
            "proxy",
            width=int(340 * self.scale),
        )
        self.ptree.column(
            "type",
            width=int(130 * self.scale),
            anchor="center",
        )
        self.ptree.column(
            "status",
            width=int(260 * self.scale),
        )
        self.ptree.column(
            "active",
            width=int(110 * self.scale),
            anchor="center",
        )

        self.ptree.grid(
            row=0,
            column=0,
            sticky="nsew",
        )

        proxy_table_scroll = ttk.Scrollbar(
            proxy_table_frame,
            orient="vertical",
            command=self.ptree.yview,
        )
        proxy_table_scroll.grid(
            row=0,
            column=1,
            sticky="ns",
        )
        self.ptree.configure(
            yscrollcommand=proxy_table_scroll.set
        )

        self.ptree.tag_configure(
            "active",
            background="#d7f5d7",
        )
        self.ptree.tag_configure(
            "testing",
            background="#fff1bd",
        )
        self.ptree.tag_configure(
            "failed",
            background="#f7d7d7",
        )

        ttk.Label(
            proxy_status_tab,
            text="Proxy activity:",
            style="Header.TLabel",
        ).grid(
            row=3,
            column=0,
            sticky="w",
            pady=(pad, 3),
        )

        proxy_detail_frame = ttk.Frame(proxy_status_tab)
        proxy_detail_frame.grid(
            row=4,
            column=0,
            sticky="nsew",
        )
        proxy_detail_frame.columnconfigure(0, weight=1)
        proxy_detail_frame.rowconfigure(0, weight=1)

        self.proxy_detail_text = tk.Text(
            proxy_detail_frame,
            wrap="word",
            font=self.font_mono,
            state="disabled",
            bg=self.theme["field"],
            fg=self.theme["text"],
            insertbackground=self.theme["text"],
            selectbackground=self.theme["select"],
            selectforeground="#ffffff",
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.theme["border"],
            borderwidth=0,
        )
        self.proxy_detail_text.grid(
            row=0,
            column=0,
            sticky="nsew",
        )

        proxy_detail_scroll = ttk.Scrollbar(
            proxy_detail_frame,
            orient="vertical",
            command=self.proxy_detail_text.yview,
        )
        proxy_detail_scroll.grid(
            row=0,
            column=1,
            sticky="ns",
        )
        self.proxy_detail_text.configure(
            yscrollcommand=proxy_detail_scroll.set
        )

        # ===================================================================
        # ADD TORRENT TAB
        # Entire tab width is available for source and folder controls.
        # ===================================================================
        add_tab.columnconfigure(0, weight=1)
        add_tab.rowconfigure(0, weight=1)

        add_body = ttk.Frame(
            add_tab,
            padding=(12, 10),
        )
        add_body.grid(
            row=0,
            column=0,
            sticky="nsew",
        )
        add_body.columnconfigure(0, weight=1)

        ttk.Label(
            add_body,
            text="Magnet link or .torrent path:",
            style="Header.TLabel",
        ).grid(
            row=0,
            column=0,
            sticky="w",
        )

        self.source_var = tk.StringVar()

        source_row = ttk.Frame(add_body)
        source_row.grid(
            row=1,
            column=0,
            sticky="ew",
            pady=(4, 14),
        )
        source_row.columnconfigure(0, weight=1)

        ttk.Entry(
            source_row,
            textvariable=self.source_var,
            font=self.font_normal,
            style="GetTorrent.TEntry",
        ).grid(
            row=0,
            column=0,
            sticky="ew",
            padx=(0, pad),
        )

        ttk.Button(
            source_row,
            text="Browse",
            command=self._browse_torrent,
        ).grid(
            row=0,
            column=1,
        )

        ttk.Label(
            add_body,
            text="Downloaded data folder:",
            style="Header.TLabel",
        ).grid(
            row=2,
            column=0,
            sticky="w",
        )

        self.save_var = tk.StringVar(
            value=self.settings["download_dir"]
        )
        self.save_var.trace_add(
            "write",
            lambda *_: self._schedule_input_save(),
        )

        download_folder_row = ttk.Frame(add_body)
        download_folder_row.grid(
            row=3,
            column=0,
            sticky="ew",
            pady=(4, 14),
        )
        download_folder_row.columnconfigure(0, weight=1)

        ttk.Entry(
            download_folder_row,
            textvariable=self.save_var,
            font=self.font_normal,
            style="GetTorrent.TEntry",
        ).grid(
            row=0,
            column=0,
            sticky="ew",
            padx=(0, pad),
        )

        ttk.Button(
            download_folder_row,
            text="Folder",
            command=self._browse_folder,
        ).grid(
            row=0,
            column=1,
            padx=(0, pad),
        )

        ttk.Button(
            download_folder_row,
            text="Open",
            command=self._open_download_folder,
        ).grid(
            row=0,
            column=2,
        )

        ttk.Label(
            add_body,
            text="Torrent files folder:",
            style="Header.TLabel",
        ).grid(
            row=4,
            column=0,
            sticky="w",
        )

        self.torrent_dir_var = tk.StringVar(
            value=self.settings["torrent_dir"]
        )
        self.torrent_dir_var.trace_add(
            "write",
            lambda *_: self._schedule_input_save(),
        )

        torrent_folder_row = ttk.Frame(add_body)
        torrent_folder_row.grid(
            row=5,
            column=0,
            sticky="ew",
            pady=(4, 14),
        )
        torrent_folder_row.columnconfigure(0, weight=1)

        ttk.Entry(
            torrent_folder_row,
            textvariable=self.torrent_dir_var,
            font=self.font_normal,
            style="GetTorrent.TEntry",
        ).grid(
            row=0,
            column=0,
            sticky="ew",
            padx=(0, pad),
        )

        ttk.Button(
            torrent_folder_row,
            text="Folder",
            command=self._browse_torrent_folder,
        ).grid(
            row=0,
            column=1,
            padx=(0, pad),
        )

        ttk.Button(
            torrent_folder_row,
            text="Open",
            command=self._open_torrent_folder,
        ).grid(
            row=0,
            column=2,
        )

        ttk.Button(
            add_body,
            text="Add to queue",
            command=self._add_torrent,
        ).grid(
            row=6,
            column=0,
            sticky="w",
        )

        # ===================================================================
        # APP STATUS — OUTSIDE ALL TABS, ACROSS THE BOTTOM
        # ===================================================================
        app_status_frame = ttk.LabelFrame(
            root,
            text="App Status:",
            padding=pad,
        )
        app_status_frame.grid(
            row=1,
            column=0,
            sticky="ew",
        )
        app_status_frame.columnconfigure(0, weight=1)

        app_status_header = ttk.Frame(app_status_frame)
        app_status_header.grid(
            row=0,
            column=0,
            sticky="ew",
            pady=(0, 4),
        )
        app_status_header.columnconfigure(0, weight=1)

        self.info_var = tk.StringVar(value="Idle.")

        ttk.Label(
            app_status_header,
            textvariable=self.info_var,
            style="Small.TLabel",
            anchor="w",
        ).grid(
            row=0,
            column=0,
            sticky="ew",
        )

        ttk.Button(
            app_status_header,
            text="Clear",
            command=self._clear_status_log,
        ).grid(
            row=0,
            column=1,
            sticky="e",
            padx=(pad, 0),
        )

        app_status_body = ttk.Frame(app_status_frame)
        app_status_body.grid(
            row=1,
            column=0,
            sticky="ew",
        )
        app_status_body.columnconfigure(0, weight=1)

        self.status_text = tk.Text(
            app_status_body,
            height=8,
            wrap="word",
            font=self.font_mono,
            state="disabled",
            bg=self.theme["field"],
            fg=self.theme["text"],
            insertbackground=self.theme["text"],
            selectbackground=self.theme["select"],
            selectforeground="#ffffff",
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.theme["border"],
            borderwidth=0,
        )
        self.status_text.grid(
            row=0,
            column=0,
            sticky="ew",
        )

        app_status_scroll = ttk.Scrollbar(
            app_status_body,
            orient="vertical",
            command=self.status_text.yview,
        )
        app_status_scroll.grid(
            row=0,
            column=1,
            sticky="ns",
        )
        self.status_text.configure(
            yscrollcommand=app_status_scroll.set
        )

        self.status_text.tag_configure(
            "ERROR",
            foreground="#ff8d95",
        )
        self.status_text.tag_configure(
            "PROXY",
            foreground="#f0c96d",
        )
        self.status_text.tag_configure(
            "TORRENT",
            foreground="#84bce8",
        )
        self.status_text.tag_configure(
            "OK",
            foreground="#77df8c",
        )
        self.status_text.tag_configure(
            "INFO",
            foreground=self.theme["text"],
        )

        # Restore the user's last visible tab. Older builds used
        # 0=Downloads, 1=Proxies, 2=Add Torrent.
        try:
            old_tab = int(self.settings.get("main_tab", 0))
            self.main_notebook.select(1 if old_tab >= 2 else 0)
        except Exception:
            self.main_notebook.select(0)

        # Proxy routing was removed from the user-facing app. Force direct mode
        # even if an older settings.json still contains proxy configuration.
        self.use_proxy_var.set(False)
        self.engine.use_proxy = False
        self.engine.proxy_mode = "Auto"

        # Re-apply the selected palette after every themed widget exists.
        self._apply_theme_colors()
        self.after_idle(self._apply_windows_titlebar_theme)
        self.after(120, self._layout_progress_overlays)
        self.after(180, self._animate_progress_overlays)

        self._append_status(
            (
                f"Application ready. UI DPI={self.window_dpi} "
                f"(Windows scale about {self.dpi_scale * 100:.0f}%). "
                "Main text=13pt; all secondary text is at least 12pt."
            ),
            "INFO",
        )

        if lt is None:
            self._append_status(
                "libtorrent not installed — run: pip install libtorrent",
                "ERROR",
            )

    def _append_proxy_detail(self, message):
        """Append proxy-specific narrative activity inside the Proxies frame."""
        message = str(message or "").strip()
        if not message or not hasattr(self, "proxy_detail_text"):
            return
        timestamp = time.strftime("%H:%M:%S")
        try:
            self.proxy_detail_text.configure(state="normal")
            self.proxy_detail_text.insert("end", f"[{timestamp}] {message}\n")
            try:
                line_count = int(
                    self.proxy_detail_text.index("end-1c").split(".")[0]
                )
                if line_count > 500:
                    self.proxy_detail_text.delete("1.0", "100.0")
            except Exception:
                pass
            self.proxy_detail_text.see("end")
            self.proxy_detail_text.configure(state="disabled")
        except Exception:
            pass

    def _append_status(self, message, level="INFO"):
        message = str(message or "").strip()
        if not message:
            return

        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}\n"
        self.info_var.set(message)

        try:
            # IMPORTANT: new log lines must not yank the scrollbar away from
            # text the user is currently reading. Follow live output only when
            # the view was already at (or extremely near) the bottom.
            try:
                view_top, view_bottom = self.status_text.yview()
                follow_live = view_bottom >= 0.995
            except Exception:
                view_top = 0.0
                follow_live = True

            self.status_text.configure(state="normal")
            tag = (
                level
                if level in ("ERROR", "PROXY", "TORRENT", "OK")
                else "INFO"
            )
            self.status_text.insert("end", line, tag)

            # Only prune old lines while live-following. If the user is reading
            # older text, leave the buffer alone so deleting from the beginning
            # cannot shift their reading position underneath them.
            if follow_live:
                try:
                    line_count = int(
                        self.status_text.index("end-1c").split(".")[0]
                    )
                    if line_count > 1200:
                        self.status_text.delete("1.0", "200.0")
                except Exception:
                    pass

                self.status_text.see("end")
            else:
                # Preserve the user's current viewport. New text keeps
                # accumulating below without moving what they are reading.
                try:
                    self.status_text.yview_moveto(view_top)
                except Exception:
                    pass

            self.status_text.configure(state="disabled")
        except Exception:
            pass

    def _clear_status_log(self):
        try:
            self.status_text.configure(state="normal")
            self.status_text.delete("1.0", "end")
            self.status_text.configure(state="disabled")
            self.info_var.set("Status log cleared.")
        except Exception:
            pass

    def _torrent_queue_snapshot(self):
        records = []
        with self.engine.lock:
            items = list(self.engine.items.values())

        for item in items:
            # Never discard failed queue entries. Even a torrent that never
            # received a libtorrent handle remains until the user removes it.
            records.append({
                "source": item.source,
                "save_path": item.save_path,
                "torrent_dir": item.torrent_dir,
                "torrent_file_path": item.torrent_file_path,
                "torrent_file_saved": bool(item.torrent_file_saved),
                "name": item.name,
                "state": item.state,
                "error": getattr(item, "error", ""),
                "identity": getattr(item, "identity", ""),
                # Local UI/cache information. This is intentionally enough to
                # display completed torrents without re-adding them to
                # libtorrent or contacting trackers/DHT/peers.
                "progress": float(getattr(item, "progress", 0.0)),
                "total": int(getattr(item, "total", 0) or 0),
                "downloaded": int(getattr(item, "downloaded", 0) or 0),
                "piece_len": int(getattr(item, "piece_len", 0) or 0),
                "files": list(getattr(item, "files", []) or []),
            })
        return records

    def _persist_torrent_queue(self):
        if (
            self._restoring_queue
            or not getattr(self, "_queue_restore_complete", False)
        ):
            return
        try:
            save_torrent_queue(self._torrent_queue_snapshot())
        except Exception as e:
            if hasattr(self, "status_text"):
                self._append_status(
                    f"Could not save torrent queue state: {e}",
                    "ERROR",
                )

    def _find_saved_descriptor_for_record(self, record):
        """Recover a saved .torrent locally by swarm identity.

        Older/stale queue records may have an empty descriptor path even though
        metadata was already saved in the configured Torrent Files folder.
        This scan is local-only and never contacts trackers, DHT, or peers.
        """
        descriptor = str(record.get("torrent_file_path") or "").strip()
        if descriptor and os.path.isfile(descriptor):
            return os.path.abspath(descriptor)

        identity = str(record.get("identity") or "").strip()
        original_source = str(record.get("source") or "").strip()
        if not identity and original_source:
            identity = torrent_identity_from_source(original_source)

        torrent_dir = str(
            record.get("torrent_dir")
            or self.torrent_dir_var.get()
            or self.settings["torrent_dir"]
        ).strip()

        if not identity or not torrent_dir or not os.path.isdir(torrent_dir):
            return ""

        try:
            for name in os.listdir(torrent_dir):
                if not name.lower().endswith(".torrent"):
                    continue
                candidate = os.path.join(torrent_dir, name)
                if not os.path.isfile(candidate):
                    continue
                try:
                    candidate_identity = torrent_identity_from_source(candidate)
                except Exception:
                    candidate_identity = ""
                if candidate_identity and candidate_identity == identity:
                    return os.path.abspath(candidate)
        except Exception:
            pass

        return ""

    def _recover_finished_history_from_saved_torrents(self):
        """Recover completed history from saved .torrent files, locally only."""
        if lt is None:
            return []

        torrent_dir = os.path.abspath(
            os.path.expandvars(
                self.torrent_dir_var.get().strip()
                or self.settings["torrent_dir"]
            )
        )
        save_path = os.path.abspath(
            os.path.expandvars(
                self.save_var.get().strip()
                or self.settings["download_dir"]
            )
        )

        if not os.path.isdir(torrent_dir) or not os.path.isdir(save_path):
            return []

        records = []
        seen = set()

        for root, _dirs, names in os.walk(torrent_dir):
            for filename in names:
                if not filename.lower().endswith(".torrent"):
                    continue

                descriptor = os.path.abspath(
                    os.path.join(root, filename)
                )

                try:
                    ti = lt.torrent_info(descriptor)
                    fs = ti.files()
                    payload_ok = True
                    payload_files = 0

                    for i in range(fs.num_files()):
                        try:
                            flags = fs.file_flags(i)
                            pad_flag = getattr(
                                getattr(lt, "file_storage", None),
                                "flag_pad_file",
                                0,
                            )
                            if pad_flag and (flags & pad_flag):
                                continue
                        except Exception:
                            pass

                        try:
                            relative = fs.file_path(i)
                        except Exception:
                            relative = fs.file_name(i)

                        relative = str(relative or "").replace("/", os.sep)
                        if not relative:
                            payload_ok = False
                            break

                        full_path = os.path.abspath(
                            os.path.join(save_path, relative)
                        )
                        try:
                            if os.path.commonpath(
                                [save_path, full_path]
                            ) != save_path:
                                payload_ok = False
                                break
                        except Exception:
                            payload_ok = False
                            break

                        expected_size = int(fs.file_size(i))
                        payload_files += 1

                        if (
                            not os.path.isfile(full_path)
                            or os.path.getsize(full_path) != expected_size
                        ):
                            payload_ok = False
                            break

                    if not payload_ok or payload_files <= 0:
                        continue

                    identity = torrent_identity_from_source(descriptor)
                    identity_key = identity or os.path.normcase(descriptor)
                    if identity_key in seen:
                        continue
                    seen.add(identity_key)

                    total = int(ti.total_size())
                    records.append({
                        "source": descriptor,
                        "save_path": save_path,
                        "torrent_dir": torrent_dir,
                        "torrent_file_path": descriptor,
                        "torrent_file_saved": True,
                        "name": str(ti.name() or filename),
                        "state": "finished",
                        "error": "",
                        "identity": identity,
                        "progress": 1.0,
                        "total": total,
                        "downloaded": total,
                        "piece_len": int(ti.piece_length()),
                        "files": [],
                    })

                except Exception as e:
                    log_external_error(
                        "Could not inspect saved torrent metadata while "
                        "recovering completed history.",
                        exc=e,
                        details=f"descriptor={descriptor!r}",
                    )

        return records

    def _complete_queue_restore(self):
        self._restoring_queue = False
        self._queue_restore_complete = True
        self._persist_torrent_queue()

        pending = list(
            getattr(self, "_pending_external_sources", []) or []
        )
        self._pending_external_sources.clear()

        for index, (source, origin) in enumerate(pending):
            self.after(
                50 + (index * 100),
                lambda s=source, o=origin: self._enqueue_external_source(s, o),
            )

    def _restore_local_queue_record(self, record, state):
        """Restore a non-active queue entry without starting any networking."""
        original_source = str(record.get("source") or "").strip()
        descriptor = self._find_saved_descriptor_for_record(record)
        save_path = str(
            record.get("save_path")
            or self.save_var.get()
            or self.settings["download_dir"]
        )
        torrent_dir = str(
            record.get("torrent_dir")
            or self.torrent_dir_var.get()
            or self.settings["torrent_dir"]
        )

        item = TorrentItem(
            original_source or descriptor,
            save_path,
            torrent_dir,
        )
        item.source = original_source or descriptor
        item.name = str(
            record.get("name")
            or initial_display_name(item.source)
        )
        item.state = state
        item.error = str(record.get("error") or "")
        item.identity = str(record.get("identity") or "").strip()
        if not item.identity:
            item.identity = torrent_identity_from_source(
                original_source or descriptor
            )
        item.torrent_file_path = descriptor
        item.torrent_file_saved = bool(
            descriptor and os.path.isfile(descriptor)
        )
        if descriptor:
            record["torrent_file_path"] = descriptor
            record["torrent_file_saved"] = True
        item.handle = None

        # First use the queue's cached display metadata when available.
        try:
            item.progress = float(record.get("progress", 0.0) or 0.0)
        except Exception:
            item.progress = 0.0
        try:
            item.total = int(record.get("total", 0) or 0)
        except Exception:
            item.total = 0
        try:
            item.downloaded = int(record.get("downloaded", 0) or 0)
        except Exception:
            item.downloaded = 0
        try:
            item.piece_len = int(record.get("piece_len", 0) or 0)
        except Exception:
            item.piece_len = 0

        cached_files = record.get("files", [])
        if isinstance(cached_files, list):
            item.files = [
                f for f in cached_files
                if isinstance(f, dict)
            ]

        # Backward compatibility with queue files written by older builds:
        # parse the SAVED LOCAL .torrent metadata to rebuild name/size/files.
        # This is purely local parsing; no libtorrent session is created.
        if descriptor and os.path.isfile(descriptor) and lt is not None:
            try:
                ti = lt.torrent_info(descriptor)
                if not record.get("name") and ti.name():
                    item.name = ti.name()

                item.total = int(ti.total_size())
                item.piece_len = int(ti.piece_length())
                fs = ti.files()
                files = []

                for i in range(fs.num_files()):
                    size = int(fs.file_size(i))
                    offset = int(fs.file_offset(i))
                    first_piece = (
                        offset // item.piece_len
                        if item.piece_len
                        else 0
                    )
                    last_byte = offset + max(size - 1, 0)
                    last_piece = (
                        last_byte // item.piece_len
                        if item.piece_len
                        else 0
                    )
                    try:
                        relative_path = fs.file_path(i)
                    except Exception:
                        relative_path = fs.file_name(i)

                    files.append({
                        "name": fs.file_name(i),
                        "path": relative_path,
                        "size": size,
                        "progress": 1.0 if state == "finished" else 0.0,
                        "first_piece": int(first_piece),
                        "last_piece": int(last_piece),
                    })

                item.files = files

                if state == "finished":
                    try:
                        item.piece_map = [True] * int(ti.num_pieces())
                    except Exception:
                        item.piece_map = []
            except Exception as e:
                # The queue entry still remains visible even if the local
                # metadata file cannot be parsed.
                self._append_status(
                    f"Could not read local metadata for {item.name}: "
                    f"{type(e).__name__}: {e}",
                    "ERROR",
                )

        if state == "finished":
            item.progress = 1.0
            item.downloaded = item.total
            item.dl_rate = 0.0
            item.network_rate = 0.0
            item.payload_total = 0
            item.peers = 0

            # If files came from an old cached queue, make them visually 100%.
            for f in item.files:
                try:
                    f["progress"] = 1.0
                except Exception:
                    pass

            if not item.piece_map and item.files:
                highest_piece = max(
                    int(f.get("last_piece", -1))
                    for f in item.files
                )
                if highest_piece >= 0:
                    item.piece_map = [True] * (highest_piece + 1)

        with self.engine.lock:
            self.engine.items[item.uid] = item

        return item

    def _restore_torrent_queue(self):
        if self._queue_restore_done or self._restoring_queue:
            return
        self._queue_restore_done = True

        records = load_torrent_queue()
        if not records:
            records = self._recover_finished_history_from_saved_torrents()
            if records:
                noun = "entry" if len(records) == 1 else "entries"
                self._append_status(
                    (
                        f"Recovered {len(records)} completed torrent history "
                        f"{noun} from saved local .torrent metadata."
                    ),
                    "OK",
                )
            else:
                self._append_status(
                    "No previous torrent queue to restore.",
                    "INFO",
                )
                self._complete_queue_restore()
                return

        self._restoring_queue = True
        restored = 0
        active_to_resume = 0
        offline_finished = 0
        offline_failed = 0

        try:
            self._append_status(
                f"Restoring {len(records)} torrent(s) from the previous session...",
                "TORRENT",
            )

            active_records = []

            # Finished and failed torrents are queue/history records. Showing
            # them does not require a network session, tracker, DHT, peer test,
            # proxy test, or re-adding the torrent to libtorrent.
            for record in records:
                previous_state = str(
                    record.get("state") or "downloading"
                )

                if previous_state == "finished":
                    item = self._restore_local_queue_record(
                        record,
                        "finished",
                    )
                    restored += 1
                    offline_finished += 1
                    self._append_status(
                        f"Restored finished torrent offline: {item.name}",
                        "OK",
                    )
                    continue

                if previous_state == "error":
                    item = self._restore_local_queue_record(
                        record,
                        "error",
                    )
                    if not item.error:
                        item.error = "Previous download attempt failed."
                    restored += 1
                    offline_failed += 1
                    self._append_status(
                        f"Restored failed torrent offline: "
                        f"{item.name} | {item.error} "
                        f"(no automatic retry; use right-click Retry)",
                        "ERROR",
                    )
                    continue

                active_records.append(record)

            # Common case for a completed-download history: stop here.
            # No libtorrent session has been created and no networking occurred.
            if not active_records:
                self._refresh_torrents()
                self._append_status(
                    (
                        f"Restore complete: {offline_finished} finished and "
                        f"{offline_failed} failed torrent(s) loaded locally. "
                        "No torrent network session was started."
                    ),
                    "OK",
                )
                return

            # Only records that were not Finished/Failed reach this point.
            # The application is direct-only; active records resume directly.
            self.engine.use_proxy = False
            self.engine.proxy_mode = "Auto"

            probe_source = ""
            for record in active_records:
                descriptor = self._find_saved_descriptor_for_record(record)
                if (
                    descriptor
                    and descriptor
                    != str(record.get("torrent_file_path") or "").strip()
                ):
                    record["torrent_file_path"] = descriptor
                    record["torrent_file_saved"] = True
                    self._append_status(
                        f"Recovered saved torrent metadata locally: "
                        f"{descriptor}",
                        "OK",
                    )
                original_source = str(
                    record.get("source") or ""
                ).strip()

                if descriptor and os.path.isfile(descriptor):
                    probe_source = descriptor
                    break
                if original_source.startswith("magnet:"):
                    probe_source = original_source
                    break
                if (
                    original_source
                    and os.path.isfile(original_source)
                ):
                    probe_source = original_source
                    break

            if not self.engine.ensure_started(
                source=probe_source or None
            ):
                # Keep every record visible. A startup/network problem must
                # never make a saved torrent disappear from the queue.
                for record in active_records:
                    item = self._restore_local_queue_record(
                        record,
                        "error",
                    )
                    item.error = (
                        "Could not resume after restart because the configured "
                        "direct download engine/network session could not start."
                    )
                    restored += 1

                self._refresh_torrents()
                self._append_status(
                    (
                        "Active torrents could not resume, so they remain in "
                        "the list as Failed. Finished torrents were restored "
                        "locally without networking."
                    ),
                    "ERROR",
                )
                return

            for record in active_records:
                original_source = str(
                    record.get("source") or ""
                ).strip()
                descriptor = str(
                    record.get("torrent_file_path") or ""
                ).strip()
                save_path = str(
                    record.get("save_path")
                    or self.save_var.get()
                    or self.settings["download_dir"]
                )
                torrent_dir = str(
                    record.get("torrent_dir")
                    or self.torrent_dir_var.get()
                    or self.settings["torrent_dir"]
                )
                previous_state = str(
                    record.get("state") or "downloading"
                )

                restore_source = ""
                if descriptor and os.path.isfile(descriptor):
                    restore_source = descriptor
                elif original_source.startswith("magnet:"):
                    restore_source = original_source
                elif (
                    original_source
                    and os.path.isfile(original_source)
                ):
                    restore_source = original_source

                if not restore_source:
                    failed = self._restore_local_queue_record(
                        record,
                        "error",
                    )
                    failed.error = "Saved .torrent/source is missing."
                    restored += 1
                    self._append_status(
                        f"Restored as Failed: {failed.name} | "
                        f"{failed.error}",
                        "ERROR",
                    )
                    continue

                try:
                    os.makedirs(save_path, exist_ok=True)
                    os.makedirs(torrent_dir, exist_ok=True)

                    item = self.engine.add_torrent(
                        restore_source,
                        save_path,
                        torrent_dir,
                    )

                    if original_source:
                        item.source = original_source
                    if descriptor:
                        item.torrent_file_path = descriptor
                        item.torrent_file_saved = os.path.isfile(
                            descriptor
                        )
                    if record.get("name"):
                        item.name = str(record.get("name"))

                    if item.handle is None:
                        item.state = "error"
                        if not item.error:
                            item.error = (
                                "Failed while restoring torrent."
                            )
                        restored += 1
                        continue

                    if previous_state in ("paused", "stopped"):
                        try:
                            item.handle.unset_flags(
                                lt.torrent_flags.auto_managed
                            )
                        except Exception:
                            pass
                        try:
                            item.handle.pause()
                        except Exception:
                            pass
                        item.state = previous_state

                    else:
                        try:
                            item.handle.set_flags(
                                lt.torrent_flags.auto_managed
                            )
                            item.handle.pause()
                            item.handle.set_upload_mode(False)
                        except Exception:
                            pass
                        item.state = "queued"
                        self.engine._last_progress_time[item.uid] = (
                            item.progress,
                            time.time(),
                        )
                        active_to_resume += 1

                    restored += 1

                except Exception as e:
                    failed = self._restore_local_queue_record(
                        record,
                        "error",
                    )
                    failed.error = (
                        f"Restore failed: "
                        f"{type(e).__name__}: {e}"
                    )
                    restored += 1
                    self._append_status(
                        f"Restored as Failed: {failed.name} | "
                        f"{failed.error}",
                        "ERROR",
                    )

            self._refresh_torrents()
            self._append_status(
                (
                    f"Restore complete: {offline_finished} finished torrent(s) "
                    f"loaded offline; {active_to_resume} active torrent(s) "
                    f"resumed."
                ),
                "OK" if restored else "ERROR",
            )

        finally:
            self._complete_queue_restore()

    # --- source buttons ----------------------------------------------------
    def _browse_torrent(self):
        path = filedialog.askopenfilename(
            filetypes=[("Torrent files", "*.torrent"), ("All files", "*.*")])
        if path:
            self.source_var.set(path)
            self._append_status(f"Selected torrent file: {path}", "TORRENT")

    def _browse_folder(self):
        path = filedialog.askdirectory()
        if path:
            self.save_var.set(path)
            self._persist_inputs()
            self._append_status(f"Download folder set to: {path}", "INFO")

    def _browse_torrent_folder(self):
        path = filedialog.askdirectory()
        if path:
            self.torrent_dir_var.set(path)
            self._persist_inputs()
            self._append_status(f"Torrent-files folder set to: {path}", "INFO")

    def _open_folder(self, path):
        path = (path or "").strip()
        if not path:
            return
        try:
            os.makedirs(path, exist_ok=True)
            full_path = os.path.abspath(path)
            if sys.platform == "win32":
                os.startfile(full_path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", full_path])
            else:
                subprocess.Popen(["xdg-open", full_path])
        except Exception as e:
            messagebox.showerror("Open folder", f"Could not open folder:\n\n{e}")

    def _open_download_folder(self):
        self._open_folder(self.save_var.get())

    def _open_torrent_folder(self):
        self._open_folder(self.torrent_dir_var.get())

    def _on_queue_limit_changed(self, event=None):
        try:
            value = int(self.queue_limit_var.get())
        except Exception:
            value = 5
        value = max(1, min(100, value))
        self.queue_limit_var.set(value)
        self.engine.set_max_active_downloads(value)
        self._schedule_input_save()
        if hasattr(self, "status_text"):
            self._append_status(
                f"Concurrent download limit set to {value}.",
                "TORRENT",
            )

    def _track_window_geometry(self, event=None):
        if getattr(self, "_exiting", False):
            return
        try:
            state = self.state()
        except Exception:
            return
        if state == "zoomed":
            self._last_visible_state = "zoomed"
        elif state == "normal":
            self._last_visible_state = "normal"
            try:
                geometry = self.geometry()
                if re.match(r"^\\d+x\\d+[-+]\\d+[-+]\\d+$", geometry):
                    self._last_normal_geometry = geometry
            except Exception:
                pass

    def _apply_saved_window_state(self):
        state = self.settings.get("window_state", "zoomed")
        try:
            if state == "zoomed":
                self.state("zoomed")
                self._last_visible_state = "zoomed"
            else:
                self.state("normal")
                geometry = self.settings.get("window_geometry", "")
                if geometry:
                    self.geometry(geometry)
                self._last_visible_state = "normal"
        except Exception:
            try:
                self.state("zoomed")
                self._last_visible_state = "zoomed"
            except Exception:
                pass

    def _make_tray_image(self):
        if Image is None:
            return None
        try:
            image = Image.open(
                resource_path("GetTorrent.ico")
            ).convert("RGBA")
            return image
        except Exception:
            return None

    def _open_buy_me_a_coffee(self):
        try:
            webbrowser.open(BUY_ME_A_COFFEE_URL, new=2)
            self._append_status(
                f"Opened Buy Me a Coffee: {BUY_ME_A_COFFEE_URL}",
                "INFO",
            )
        except Exception as e:
            messagebox.showerror(
                "Buy Me a Coffee",
                (
                    "Could not open the Buy Me a Coffee page:\n\n"
                    f"{BUY_ME_A_COFFEE_URL}\n\n{e}"
                ),
            )

    def _show_about_dialog(self):
        dialog = tk.Toplevel(self)
        dialog.title("About GetTorrent")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.configure(bg=self.theme["bg"])

        try:
            dialog.iconbitmap(resource_path("GetTorrent.ico"))
        except Exception:
            pass

        frame = ttk.Frame(dialog, padding=(26, 22, 26, 22))
        frame.grid(row=0, column=0, sticky="nsew")

        ttk.Label(
            frame,
            text="GetTorrent",
            font=("Segoe UI", 18, "bold"),
        ).grid(row=0, column=0, sticky="w")

        ttk.Label(
            frame,
            text=(
                "A Windows torrent downloading app with queue management, "
                "DHT discovery, persistent download history, payload-only "
                "speed reporting, and no seeding after completion."
            ),
            wraplength=560,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(10, 14))

        ttk.Label(
            frame,
            text=(
                "☕ If you find GetTorrent useful, you can support "
                "continued development through Buy Me a Coffee."
            ),
            wraplength=560,
            justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(0, 6))

        coffee_link = tk.Label(
            frame,
            text="☕ buymeacoffee.com/davekaiser",
            bg=self.theme["bg"],
            fg=self.theme["accent2"],
            activeforeground=self.theme["accent"],
            activebackground=self.theme["bg"],
            cursor="hand2",
            font=("Segoe UI", 12, "underline"),
        )
        coffee_link.grid(row=3, column=0, sticky="w", pady=(0, 14))
        coffee_link.bind(
            "<Button-1>",
            lambda _event: self._open_buy_me_a_coffee(),
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)

        coffee_button = ttk.Button(
            buttons,
            text="☕ Buy Me a Coffee",
            command=self._open_buy_me_a_coffee,
            cursor="hand2",
        )
        coffee_button.grid(row=0, column=0, sticky="w")

        ttk.Button(
            buttons,
            text="Close",
            command=dialog.destroy,
        ).grid(row=0, column=1, sticky="e", padx=(16, 0))

        dialog.update_idletasks()
        try:
            x = self.winfo_rootx() + max(
                0,
                (self.winfo_width() - dialog.winfo_reqwidth()) // 2,
            )
            y = self.winfo_rooty() + max(
                0,
                (self.winfo_height() - dialog.winfo_reqheight()) // 2,
            )
            dialog.geometry(f"+{x}+{y}")
        except Exception:
            pass

        dialog.grab_set()
        dialog.focus_force()

    def _setup_tray_icon(self):
        if pystray is None:
            self._append_status(
                "System tray support is unavailable; X will minimize to the taskbar.",
                "ERROR",
            )
            return
        try:
            image = self._make_tray_image()
            if image is None:
                return

            def tray_show(icon, item):
                try:
                    self.after(0, self._show_app)
                except Exception:
                    pass

            def tray_exit(icon, item):
                try:
                    self.after(0, self._exit_app)
                except Exception:
                    pass

            menu = pystray.Menu(
                pystray.MenuItem("Show App", tray_show, default=True),
                pystray.MenuItem("Exit App", tray_exit),
            )
            self.tray_icon = pystray.Icon(
                "TorrentDownloader",
                image,
                "GetTorrent",
                menu,
            )
            threading.Thread(
                target=self.tray_icon.run,
                daemon=True,
                name="TorrentDownloaderTray",
            ).start()
        except Exception as e:
            self.tray_icon = None
            self._append_status(
                f"Could not start system tray icon: {type(e).__name__}: {e}",
                "ERROR",
            )

    def _hide_to_tray(self):
        if getattr(self, "_exiting", False):
            return
        self._track_window_geometry()
        self._persist_inputs()
        if self.tray_icon is None:
            try:
                self.iconify()
            except Exception:
                pass
            self._append_status(
                "System tray icon unavailable; minimized to taskbar instead.",
                "ERROR",
            )
            return
        try:
            self.withdraw()
            self._append_status(
                "Window minimized to system tray. Use the tray icon or File > Exit App to close.",
                "INFO",
            )
        except Exception:
            pass

    def _force_foreground_windows(self):
        if sys.platform != "win32":
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            HWND = ctypes.c_void_p
            BOOL = ctypes.c_int
            INT = ctypes.c_int

            user32.ShowWindow.argtypes = [HWND, INT]
            user32.ShowWindow.restype = BOOL
            user32.BringWindowToTop.argtypes = [HWND]
            user32.BringWindowToTop.restype = BOOL
            user32.SetForegroundWindow.argtypes = [HWND]
            user32.SetForegroundWindow.restype = BOOL

            hwnd_value = int(self.winfo_id())
            if hwnd_value <= 0:
                return

            hwnd = HWND(hwnd_value)
            user32.ShowWindow(hwnd, 9)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        except Exception as e:
            log_external_error(
                "Could not foreground the existing application window.",
                exc=e,
            )

    def _show_app(self):
        if getattr(self, "_exiting", False):
            return

        try:
            current_state = str(self.state() or "").lower()
        except Exception:
            current_state = ""

        # If already visible, do not force window-state transitions just
        # because a browser forwarded a magnet.
        if current_state in ("withdrawn", "iconic"):
            try:
                self.deiconify()
                if self._last_visible_state == "zoomed":
                    self.state("zoomed")
                else:
                    self.state("normal")
                    if self._last_normal_geometry:
                        self.geometry(self._last_normal_geometry)
            except Exception as e:
                log_external_error(
                    "Could not restore the existing application window.",
                    exc=e,
                )

        try:
            self.lift()
            self.focus_force()
        except Exception as e:
            log_external_error(
                "Could not raise/focus the existing application window.",
                exc=e,
            )

        try:
            self.after(20, self._force_foreground_windows)
        except Exception as e:
            log_external_error(
                "Could not schedule foreground activation.",
                exc=e,
            )

    def _exit_app(self):
        if getattr(self, "_exiting", False):
            return
        self._exiting = True

        # If the app is currently visible, remember its last useful state.
        try:
            if self.state() in ("normal", "zoomed"):
                self._track_window_geometry()
        except Exception:
            pass

        self._persist_inputs()
        self._persist_torrent_queue()

        try:
            if self._progress_animation_job is not None:
                self.after_cancel(self._progress_animation_job)
                self._progress_animation_job = None
        except Exception:
            pass
        self._clear_progress_overlays()

        try:
            if self.tray_icon is not None:
                self.tray_icon.stop()
        except Exception:
            pass

        try:
            self.engine.stop()
        except Exception:
            pass

        try:
            self.destroy()
        except Exception:
            pass

    def _settings_snapshot(self):
        return {
            "download_dir": os.path.abspath(
                os.path.expandvars(
                    (self.save_var.get().strip() if hasattr(self, "save_var") else "")
                    or system_downloads_folder()
                )
            ),
            "torrent_dir": os.path.abspath(
                os.path.expandvars(
                    (self.torrent_dir_var.get().strip()
                     if hasattr(self, "torrent_dir_var") else "")
                    or os.path.join(system_downloads_folder(), "Torrent Files")
                )
            ),
            "use_proxy": False,
            "proxy_type": "Auto",
            "proxies": "",
            "main_tab": (
                self.main_notebook.index(self.main_notebook.select())
                if hasattr(self, "main_notebook")
                else int(self.settings.get("main_tab", 0))
            ),
            "proxy_tab": 0,
            "queue_limit": max(
                1,
                min(
                    100,
                    int(
                        self.queue_limit_var.get()
                        if hasattr(self, "queue_limit_var")
                        else self.settings.get("queue_limit", 5)
                    ),
                ),
            ),
            "window_state": (
                self._last_visible_state
                if getattr(self, "_last_visible_state", "normal")
                in ("normal", "zoomed")
                else "normal"
            ),
            "window_geometry": getattr(
                self,
                "_last_normal_geometry",
                "",
            ),
            "theme": getattr(self, "theme_name", "Slate Night"),
            "column_widths": (
                self._current_column_widths()
                if hasattr(self, "tree")
                else dict(self.settings.get("column_widths", {}))
            ),
        }

    def _persist_inputs(self):
        try:
            self.settings = self._settings_snapshot()
            save_app_settings(self.settings)
        except Exception:
            pass

    def _schedule_input_save(self, event=None):
        try:
            if getattr(self, "_input_save_job", None) is not None:
                self.after_cancel(self._input_save_job)
            self._input_save_job = self.after(500, self._persist_inputs)
        except Exception:
            pass

    def _safe_add_external_source(self, source, origin="external link"):
        log_startup_diagnostic(
            f"external add callback entered ({origin})"
        )
        try:
            normalized = normalize_external_source(source)
            log_startup_diagnostic(
                "external source normalization succeeded"
            )
            if not normalized:
                raise ValueError(
                    "The external request did not contain a valid magnet "
                    "or .torrent file."
                )

            if (
                normalized.lower().endswith(".torrent")
                and not os.path.isfile(normalized)
            ):
                raise FileNotFoundError(
                    f"Torrent file does not exist: {normalized}"
                )

            self.source_var.set(normalized)
            log_startup_diagnostic(
                "external source copied into Add Torrent source field"
            )
            self._append_status(
                (
                    f"Received {origin}: "
                    f"{initial_display_name(normalized)}"
                ),
                "TORRENT",
            )
            log_startup_diagnostic(
                "calling GUI _add_torrent() for forwarded source"
            )
            self._add_torrent()
            log_startup_diagnostic(
                "GUI _add_torrent() returned normally for forwarded source"
            )
        except Exception as e:
            log_path = log_external_error(
                f"Failed while processing {origin}.",
                exc=e,
                details=f"source={source!r}",
            )
            try:
                self._append_status(
                    (
                        f"ERROR processing {origin}: "
                        f"{type(e).__name__}: {e}"
                    ),
                    "ERROR",
                )
            except Exception:
                pass
            try:
                messagebox.showerror(
                    "Could not open torrent link",
                    (
                        "GetTorrent could not process the link, but "
                        "the application will stay open."
                        "\n\n"
                        f"{type(e).__name__}: {e}"
                        + (
                            f"\n\nDiagnostic log:\n{log_path}"
                            if log_path
                            else ""
                        )
                    ),
                )
            except Exception:
                pass

    def _enqueue_external_source(self, source, origin="browser/IPC link"):
        if not source:
            return
        try:
            normalized = normalize_external_source(source)
            if not normalized:
                raise ValueError(
                    "The browser did not provide a recognizable magnet link "
                    "or .torrent file."
                )
        except Exception as e:
            log_path = log_external_error(
                f"Rejected {origin}.",
                exc=e,
                details=f"source={source!r}",
            )
            try:
                self._show_app()
                self._append_status(
                    f"ERROR: rejected {origin}: {e}",
                    "ERROR",
                )
                messagebox.showerror(
                    "Invalid torrent link",
                    (
                        f"{e}\n\nGetTorrent will remain open."
                        + (
                            f"\n\nDiagnostic log:\n{log_path}"
                            if log_path
                            else ""
                        )
                    ),
                )
            except Exception:
                pass
            return

        if not getattr(self, "_queue_restore_complete", False):
            pending = self._pending_external_sources
            if not any(
                queued_source == normalized
                for queued_source, _queued_origin in pending
            ):
                pending.append((normalized, origin))
                self._append_status(
                    "Torrent link received; waiting for previous torrent "
                    "history restore to finish before adding it.",
                    "TORRENT",
                )
            return

        log_startup_diagnostic(
            f"external source accepted; preparing existing GUI ({origin})"
        )
        self._show_app()
        log_startup_diagnostic(
            "existing GUI foreground/restore step returned"
        )
        try:
            self.main_notebook.select(0)
        except Exception:
            pass

        # Let the window finish surfacing before any libtorrent work starts.
        self.after(
            100,
            lambda s=normalized, o=origin: self._safe_add_external_source(s, o),
        )
        log_startup_diagnostic(
            "forwarded source callback scheduled on Tk event loop"
        )

    def _poll_ipc_messages(self):
        folder = ipc_folder()
        try:
            os.makedirs(folder, exist_ok=True)
            try:
                names = sorted(
                    name
                    for name in os.listdir(folder)
                    if name.endswith(".msg")
                )
            except Exception as e:
                log_external_error(
                    "Could not enumerate IPC message folder.",
                    exc=e,
                    details=f"folder={folder!r}",
                )
                return

            for name in names:
                path = os.path.join(folder, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        source = f.read().strip()
                    os.remove(path)
                except Exception as e:
                    log_external_error(
                        "Could not read/remove IPC message.",
                        exc=e,
                        details=f"path={path!r}",
                    )
                    continue

                try:
                    if source in ("__GETTORRENT_EXIT_FOR_UPDATE__", "__TORRENTDOWNLOADER_EXIT_FOR_UPDATE__"):
                        self._append_status(
                            "Installer requested a safe application exit for update.",
                            "INFO",
                        )
                        self.after(50, self._exit_app)
                        return

                    normalized = normalize_external_source(source)
                    if normalized:
                        log_startup_diagnostic(
                            "running instance received forwarded source from IPC"
                        )
                        self._enqueue_external_source(
                            normalized,
                            origin=(
                                "browser link forwarded from another app instance"
                            ),
                        )
                    else:
                        log_external_error(
                            "IPC message did not contain a valid torrent source.",
                            details=(
                                f"path={path!r}\\n"
                                f"source={source!r}"
                            ),
                        )
                except Exception as e:
                    log_external_error(
                        "Unhandled error while processing IPC message.",
                        exc=e,
                        details=(
                            f"path={path!r}\\n"
                            f"source={source!r}"
                        ),
                    )
                    try:
                        self._append_status(
                            (
                                "ERROR processing forwarded magnet link: "
                                f"{type(e).__name__}: {e}"
                            ),
                            "ERROR",
                        )
                    except Exception:
                        pass
        except Exception as e:
            log_external_error(
                "Unexpected IPC polling failure.",
                exc=e,
                details=f"folder={folder!r}",
            )
        finally:
            try:
                self.after(350, self._poll_ipc_messages)
            except Exception as e:
                log_external_error(
                    "Could not reschedule IPC polling.",
                    exc=e,
                )


    def _set_proxy_indicator(self, connected=False, proxy="", proxy_type="",
                             detail="", disabled=False, testing=False,
                             unverified=False):
        if disabled:
            color = "#888888"
            label = "Proxy OFF"
            current = "Current proxy: none"
        elif connected:
            color = "#1aa33b"
            label = "BITTORRENT VERIFIED"
            suffix = f" ({proxy_type})" if proxy_type else ""
            current = f"Current proxy: {proxy}{suffix}"
        elif unverified:
            color = "#c58a00"
            label = "TORRENT UNVERIFIED"
            suffix = f" ({proxy_type})" if proxy_type else ""
            current = f"Transport proxy: {proxy or 'none'}{suffix}"
        else:
            color = "#d52b2b"
            label = "TESTING..." if testing else "NOT CONNECTED"
            suffix = f" ({proxy_type})" if proxy_type else ""
            current = f"Current proxy: {proxy or 'none'}{suffix}"

        try:
            self.proxy_light.configure(foreground=color)
            self.proxy_connection_var.set(label)
            self.current_proxy_var.set(current)
        except Exception:
            pass

        if detail and not disabled and hasattr(self, "info_var"):
            self.info_var.set(detail)

    def _on_proxy_type_changed(self, event=None):
        self.engine.proxy_mode = self.proxy_type_var.get()
        self._persist_inputs()
        if hasattr(self, "status_text"):
            message = f"Proxy type mode changed to {self.proxy_type_var.get()}."
            self._append_proxy_detail(message)
            self._append_status(message, "PROXY")
        if self.use_proxy_var.get() and self.proxy_text.get("1.0", "end").strip():
            self._set_proxy_indicator(
                connected=False,
                proxy=self.proxy_mgr.current_key() or "",
                testing=True,
                detail="Proxy type changed. Re-testing proxies...",
            )
            self.after(50, self._test_proxies)

    def _on_proxy_toggle(self):
        on = self.use_proxy_var.get()
        try:
            # Proxy configuration remains editable even when routing is OFF.
            # The switch controls network use, not whether the user may edit
            # or save their proxy list.
            self.proxy_text.configure(state="normal", bg="#ffffff")
            self.proxy_load_btn.configure(state="normal")
            self.proxy_file_btn.configure(state="normal")
            self.proxy_save_btn.configure(state="normal")
            self.proxy_test_btn.configure(state="normal" if on else "disabled")
            self.proxy_type_combo.configure(state="readonly")
        except Exception:
            pass

        self.engine.use_proxy = on
        self.engine.proxy_mode = self.proxy_type_var.get() if hasattr(self, "proxy_type_var") else "Auto"
        self._persist_inputs()
        if hasattr(self, "status_text"):
            message = (
                "Proxy routing setting is ON. Networking starts only when an "
                "active torrent needs it."
                if on
                else "Proxy routing setting is OFF. No torrent connection is "
                "opened just to display saved Finished/Failed entries."
            )
            self._append_proxy_detail(message)
            self._append_status(message, "PROXY")

        self.proxy_hint.configure(
            text="(on - traffic routed via proxies)" if on
            else "(off - direct connection)")

        if on:
            self._set_proxy_indicator(
                connected=False,
                proxy=self.proxy_mgr.current_key() or "",
                detail="Proxy mode enabled. Testing the saved proxy list...",
            )
            # During initial widget construction info_var does not exist yet;
            # startup performs its own delayed test after the GUI is complete.
            if hasattr(self, "info_var") and self.proxy_text.get("1.0", "end").strip():
                self.after(50, self._test_proxies)
        else:
            self._set_proxy_indicator(disabled=True)
            if self.engine.session is not None:
                self.engine._sync_network_mode(reset_connections=True)

    def _proxy_test_source(self):
        source = self.source_var.get().strip()
        if source.startswith("magnet:") or source.lower().endswith(".torrent"):
            return source

        items = self._selected_items()
        if not items:
            with self.engine.lock:
                items = list(self.engine.items.values())

        if items:
            item = items[0]
            if item.torrent_file_path and os.path.isfile(item.torrent_file_path):
                return item.torrent_file_path
            return item.source

        return ""

    def _test_proxies(self):
        if not self.use_proxy_var.get():
            self._append_status(
                "Proxy routing is OFF. Turn it on before running the torrent-specific proxy test.",
                "PROXY",
            )
            return

        source = self._proxy_test_source()
        if not source:
            self._set_proxy_indicator(
                connected=False,
                unverified=True,
                proxy=self.proxy_mgr.current_key() or "",
                detail="A torrent is required for a real BitTorrent proxy test.",
            )
            self._append_status(
                "Cannot certify a proxy for BitTorrent without a torrent. "
                "Paste a magnet link, select an existing torrent, or browse to "
                "a .torrent file, then click Test for torrent. Generic port-80/"
                "3128 tunnel success is deliberately NOT shown as green.",
                "ERROR",
            )
            return

        self.proxy_mgr.load(self.proxy_text.get("1.0", "end"))
        self.engine.use_proxy = True
        self.engine.proxy_mode = self.proxy_type_var.get()
        self._refresh_proxy_table()

        if not self.proxy_mgr.has_any():
            self._append_status("No proxy addresses are loaded.", "ERROR")
            return

        self._set_proxy_indicator(
            connected=False,
            testing=True,
            proxy="",
            detail="Running actual BitTorrent compatibility tests...",
        )
        try:
            self.proxy_notebook.select(1)
        except Exception:
            pass
        self._append_status(
            (
                f"Testing {len(self.proxy_mgr.proxies)} proxy address(es) against "
                "the ACTUAL torrent. Green requires magnet metadata or a real "
                "BitTorrent peer connection through the proxy."
            ),
            "PROXY",
        )

        def worker():
            proxy = self.engine._select_torrent_compatible_proxy(source)
            if proxy is None:
                self.event_q.put((
                    "status_detail",
                    {
                        "level": "ERROR",
                        "message": (
                            "No proxy passed the torrent-specific compatibility test."
                        ),
                    },
                ))
            else:
                key = f"{proxy[0]}:{proxy[1]}"
                kind = self.proxy_mgr.get_detected_type(key)
                self.event_q.put((
                    "status_detail",
                    {
                        "level": "OK",
                        "message": (
                            f"Torrent-compatible proxy selected: {key} ({kind})."
                        ),
                    },
                ))

        threading.Thread(target=worker, daemon=True).start()

    def _load_proxies(self):
        parsed, errors = self.proxy_mgr.load(self.proxy_text.get("1.0", "end"))
        self._persist_inputs()
        self._refresh_proxy_table()
        msg = f"Loaded {len(parsed)} proxy address(es)."
        if errors:
            msg += " Skipped: " + " | ".join(errors)
        self._append_proxy_detail(msg)
        self._append_status(msg, "PROXY")
        if parsed and self.use_proxy_var.get():
            self._test_proxies()

    def _load_proxies_file(self):
        path = filedialog.askopenfilename(
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        # The file contents always replace what is visibly in the proxy box.
        self.proxy_text.delete("1.0", "end")
        self.proxy_text.insert("1.0", content)
        self._persist_inputs()
        self._append_proxy_detail(f"Loaded proxy list file: {path}")
        self._append_status(f"Loaded proxy list file: {path}", "PROXY")
        self._load_proxies()

    def _save_proxies_file(self):
        path = filedialog.asksaveasfilename(
            title="Save proxy list",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile="proxies.txt",
        )
        if not path:
            return

        if not path.lower().endswith(".txt"):
            path += ".txt"

        try:
            content = self.proxy_text.get("1.0", "end-1c")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            self._persist_inputs()
            self._append_proxy_detail(f"Proxy list saved: {path}")
            self._append_status(f"Proxy list saved: {path}", "OK")
        except Exception as e:
            messagebox.showerror("Save proxy list", f"Could not save proxy list:\n\n{e}")

    # --- torrent actions ---------------------------------------------------
    def _select_torrent_uid(self, uid):
        self._refresh_torrents()
        for row, row_uid in self._row_to_uid.items():
            if row_uid == uid:
                try:
                    self.tree.selection_set(row)
                    self.tree.focus(row)
                    self.tree.see(row)
                    self.main_notebook.select(0)
                    self._on_select_torrent()
                except Exception:
                    pass
                return

    def _existing_torrent_for_source(self, source):
        identity = torrent_identity_from_source(source)
        if not identity:
            return None
        try:
            return self.engine._existing_identity_item(identity)
        except Exception:
            return None

    def _duplicate_prompt(self, item):
        """Ask the user what to do with a torrent already in the queue."""
        state = str(item.state or "").lower()

        if state == "finished":
            title = "Torrent already finished"
            message = (
                f"This torrent is already in the list as Finished:\n\n"
                f"{item.name}\n\n"
                "Recheck the existing downloaded files?\n\n"
                "Yes = recheck the files and download only missing/corrupt "
                "pieces.\n"
                "No = keep it exactly as it is and select the existing row."
            )
            return "recheck" if messagebox.askyesno(title, message) else "select"

        if state == "error":
            title = "Torrent already failed"
            message = (
                f"This torrent is already in the list as Failed:\n\n"
                f"{item.name}\n\n"
                "Retry the existing torrent now?\n\n"
                "Yes = reset its discovery attempts and try again.\n"
                "No = keep the Failed entry and select it."
            )
            return "reload" if messagebox.askyesno(title, message) else "select"

        if state in ("paused", "stopped"):
            title = "Torrent already paused"
            message = (
                f"This torrent is already in the list as "
                f"{state.title()}:\n\n{item.name}\n\n"
                "Resume/reload the existing torrent?\n\n"
                "Yes = restart it without deleting downloaded data.\n"
                "No = leave it paused and select the existing row."
            )
            return "reload" if messagebox.askyesno(title, message) else "select"

        title = "Torrent already in the list"
        message = (
            f"This torrent is already in the list and is currently "
            f"{state or 'active'}:\n\n{item.name}\n\n"
            "Restart/reload the existing torrent?\n\n"
            "This is normally unnecessary if it is already downloading.\n"
            "Yes = restart its torrent session without deleting downloaded "
            "data.\n"
            "No = keep the current download running and select its row."
        )
        return "reload" if messagebox.askyesno(title, message) else "select"

    def _reload_existing_from_duplicate(self, item, force_recheck=False):
        source = (
            item.torrent_file_path
            if item.torrent_file_path and os.path.isfile(item.torrent_file_path)
            else item.source
        )
        if not source:
            messagebox.showerror(
                "Cannot reload torrent",
                "The saved torrent source is no longer available.",
            )
            return

        self.engine.use_proxy = False
        self.engine.proxy_mode = "Auto"

        if not self.engine.ensure_started(source=source):
            return

        self.engine.reload_torrent(
            item.uid,
            force_recheck=force_recheck,
        )


    def _add_torrent(self):
        if lt is None:
            messagebox.showerror("Missing dependency",
                                 "libtorrent is not installed.\n\n"
                                 "Install it with:  pip install libtorrent")
            return
        source = self.source_var.get().strip()
        if not source:
            messagebox.showwarning("No source",
                                   "Enter a magnet link or choose a .torrent file.")
            return
        save_path = self.save_var.get().strip() or self.settings["download_dir"]
        torrent_dir = self.torrent_dir_var.get().strip() or self.settings["torrent_dir"]
        os.makedirs(save_path, exist_ok=True)
        os.makedirs(torrent_dir, exist_ok=True)

        # Ask before starting any new network/proxy work if this swarm already
        # exists in the application's queue.
        existing = self._existing_torrent_for_source(source)
        if existing is not None:
            action = self._duplicate_prompt(existing)
            self._select_torrent_uid(existing.uid)
            self.source_var.set("")

            if action == "recheck":
                self._append_status(
                    f"User requested recheck of existing Finished torrent: "
                    f"{existing.name}",
                    "TORRENT",
                )
                self._reload_existing_from_duplicate(
                    existing,
                    force_recheck=True,
                )
            elif action == "reload":
                self._append_status(
                    f"User requested reload/retry of existing torrent: "
                    f"{existing.name}",
                    "TORRENT",
                )
                self._reload_existing_from_duplicate(
                    existing,
                    force_recheck=False,
                )
            else:
                self._append_status(
                    f"Torrent already exists; existing row selected and left "
                    f"unchanged: {existing.name}",
                    "TORRENT",
                )
            return

        self.engine.use_proxy = False
        self.engine.proxy_mode = "Auto"

        self._append_status(
            f"Adding {'magnet' if source.startswith('magnet:') else '.torrent file'}.",
            "TORRENT",
        )

        log_startup_diagnostic(
            "GUI add: calling engine.ensure_started()"
        )
        if not self.engine.ensure_started(source=source):
            log_startup_diagnostic(
                "GUI add: engine.ensure_started() returned False"
            )
            self.engine.add_failed_placeholder(
                source,
                save_path,
                torrent_dir,
                "The torrent engine/network session could not start. "
                "See App Status for the exact error.",
            )
            self.source_var.set("")
            return

        log_startup_diagnostic(
            "GUI add: engine.ensure_started() succeeded"
        )
        log_startup_diagnostic(
            "GUI add: calling engine.add_torrent()"
        )
        self.engine.add_torrent(source, save_path, torrent_dir)
        log_startup_diagnostic(
            "GUI add: engine.add_torrent() returned"
        )
        self.source_var.set("")


    def _selected_uids(self):
        uids = []
        for row in self.tree.selection():
            uid = self._row_to_uid.get(row)
            if uid is not None:
                uids.append(uid)
        return uids

    def _selected_items(self):
        uids = self._selected_uids()
        with self.engine.lock:
            return [self.engine.items[uid] for uid in uids if uid in self.engine.items]

    def _double_click_torrent_location(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return "break"

        # Make the double-clicked row the one opened, even if a different
        # multi-selection existed previously.
        self.tree.selection_set(row)
        self.tree.focus(row)
        self._on_select_torrent()
        self._open_selected_file_location()
        return "break"

    def _show_torrent_context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
            self.tree.focus(row)

        items = self._selected_items()
        can_play = any(item.state == "paused" for item in items)
        can_pause = any(
            item.state in ("downloading", "metadata")
            for item in items
        )
        can_retry = any(
            item.state == "error" and item.handle is None
            for item in items
        )

        self.torrent_menu.entryconfig(
            "Play",
            state="normal" if can_play else "disabled",
        )
        self.torrent_menu.entryconfig(
            "Pause",
            state="normal" if can_pause else "disabled",
        )
        self.torrent_menu.entryconfig(
            "Retry",
            state="normal" if can_retry else "disabled",
        )
        self.torrent_menu.entryconfig(
            "Open file location",
            state="normal" if len(items) == 1 else "disabled",
        )

        try:
            self.torrent_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.torrent_menu.grab_release()

    def _windows_explorer_open(self, path, select=False):
        """Open a validated Windows filesystem target in Explorer.

        Explorer's command-line /select parsing can fall back to a default
        location (often Documents) when handed a malformed/missing path.
        ShellExecuteW with an explicitly quoted, already-validated target is
        much more reliable.
        """
        if sys.platform != "win32":
            raise OSError("Windows Explorer helper called off Windows")

        path = os.path.normpath(os.path.abspath(str(path or "").strip()))
        if not path:
            raise FileNotFoundError("Empty Explorer target")

        import ctypes

        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = ctypes.c_void_p

        if select:
            if not os.path.isfile(path):
                raise FileNotFoundError(path)
            params = f'/select,"{path}"'
            result = shell32.ShellExecuteW(
                None,
                "open",
                "explorer.exe",
                params,
                None,
                1,
            )
        else:
            if not os.path.isdir(path):
                raise FileNotFoundError(path)
            result = shell32.ShellExecuteW(
                None,
                "open",
                path,
                None,
                None,
                1,
            )

        # ShellExecute returns a value > 32 on success.
        result_value = int(result or 0)
        if result_value <= 32:
            raise OSError(
                f"Windows ShellExecute failed with code {result_value}"
            )

    def _existing_payload_folder(self, item, target=""):
        """Choose a real torrent download folder without falling back elsewhere."""
        save_path = os.path.normpath(os.path.abspath(item.save_path))

        # The configured torrent save path is authoritative and preferred.
        if os.path.isdir(save_path):
            return save_path

        # If the configured directory itself disappeared, only then walk up
        # from the target/save path to find an existing parent.
        probe = target or save_path
        return self._nearest_existing_folder(probe, save_path)

    def _torrent_payload_paths(self, item):
        """Return safe relative payload paths known for a torrent."""
        paths = []

        # Prefer the engine's authoritative libtorrent metadata when available.
        try:
            paths = self.engine._payload_relative_paths(item)
        except Exception:
            paths = []

        if not paths:
            for file_info in getattr(item, "files", []) or []:
                path = file_info.get("path") or file_info.get("name")
                if path:
                    paths.append(path)

        result = []
        seen = set()
        for path in paths:
            path = str(path or "").strip().replace("/", os.sep)
            if not path:
                continue
            normalized = os.path.normpath(path)
            if normalized in (".", os.curdir):
                continue
            key = os.path.normcase(normalized)
            if key not in seen:
                seen.add(key)
                result.append(normalized)
        return result

    def _safe_payload_path(self, item, relative_path):
        """Join a torrent relative path without allowing it outside save_path."""
        base = os.path.abspath(item.save_path)
        candidate = os.path.abspath(os.path.join(base, relative_path))
        try:
            if os.path.commonpath([base, candidate]) != base:
                return ""
        except Exception:
            return ""
        return candidate

    def _torrent_open_target(self, item):
        """Return ('file'|'folder', path, detail) for Explorer."""
        base = os.path.abspath(item.save_path)
        paths = self._torrent_payload_paths(item)

        # Single-file torrent: highlight that actual file in Explorer.
        if len(paths) == 1:
            target = self._safe_payload_path(item, paths[0])
            if target:
                return "file", target, paths[0]

        # Multi-file torrent: torrents normally have one common top-level
        # directory. Open that directory so its contents are immediately shown.
        if len(paths) > 1:
            top_parts = []
            for rel in paths:
                parts = [
                    part for part in rel.replace("\\", "/").split("/")
                    if part not in ("", ".", "..")
                ]
                if parts:
                    top_parts.append(parts[0])

            if top_parts:
                first = top_parts[0]
                if all(
                    os.path.normcase(part) == os.path.normcase(first)
                    for part in top_parts
                ):
                    folder = self._safe_payload_path(item, first)
                    if folder:
                        return "folder", folder, first

        # Metadata may not exist yet, or the torrent may have an unusual layout.
        # The chosen payload directory is still the correct location to show.
        return "folder", base, os.path.basename(base) or base

    def _nearest_existing_folder(self, path, fallback):
        candidate = os.path.abspath(path)
        if os.path.isfile(candidate):
            candidate = os.path.dirname(candidate)

        while candidate and not os.path.isdir(candidate):
            parent = os.path.dirname(candidate)
            if parent == candidate:
                break
            candidate = parent

        if candidate and os.path.isdir(candidate):
            return candidate

        fallback = os.path.abspath(fallback)
        if os.path.isdir(fallback):
            return fallback

        return ""

    def _open_selected_file_location(self):
        items = self._selected_items()
        if len(items) != 1:
            return

        item = items[0]
        kind, target, detail = self._torrent_open_target(item)

        try:
            if sys.platform == "win32":
                if kind == "file" and os.path.isfile(target):
                    self._windows_explorer_open(target, select=True)
                    self._append_status(
                        f"Opened file location and selected: {target}",
                        "TORRENT",
                    )
                    return

                if kind == "folder" and os.path.isdir(target):
                    self._windows_explorer_open(target, select=False)
                    self._append_status(
                        f"Opened torrent folder: {target}",
                        "TORRENT",
                    )
                    return

                # Never give Explorer a nonexistent /select target: on some
                # Windows builds that can open Documents/default Explorer
                # instead. Open this torrent's configured payload folder.
                existing = self._existing_payload_folder(item, target)
                if existing:
                    self._windows_explorer_open(existing, select=False)
                    self._append_status(
                        (
                            f"The expected torrent {kind} is not present yet: "
                            f"{target}. Opened configured download folder: "
                            f"{existing}"
                        ),
                        "TORRENT",
                    )
                    return

                raise FileNotFoundError(target)

            # Development/non-Windows fallback.
            if kind == "folder":
                folder = target
            else:
                folder = os.path.dirname(target)

            folder = self._nearest_existing_folder(folder, item.save_path)
            if not folder:
                raise FileNotFoundError(target)

            if sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])

            self._append_status(
                f"Opened torrent location: {folder}",
                "TORRENT",
            )

        except Exception as e:
            message = (
                f"Could not open file location for {item.name}:\n\n"
                f"{type(e).__name__}: {e}"
            )
            self._append_status(message.replace("\n\n", " | "), "ERROR")
            messagebox.showerror("Open file location", message)

    def _open_file_entry_location(self, item, file_info):
        relative_path = str(
            file_info.get("path") or file_info.get("name") or ""
        ).strip()
        if not relative_path:
            return

        target = self._safe_payload_path(item, relative_path)
        if not target:
            message = (
                f"Could not resolve a safe file location for "
                f"{file_info.get('name') or relative_path}."
            )
            self._append_status(message, "ERROR")
            messagebox.showerror("Open file location", message)
            return

        try:
            if sys.platform == "win32":
                if os.path.isfile(target):
                    self._windows_explorer_open(target, select=True)
                    self._append_status(
                        f"Opened file location and selected: {target}",
                        "TORRENT",
                    )
                    return

                if os.path.isdir(target):
                    self._windows_explorer_open(target, select=False)
                    self._append_status(
                        f"Opened folder: {target}",
                        "TORRENT",
                    )
                    return

                existing = self._existing_payload_folder(item, target)
                if existing:
                    self._windows_explorer_open(existing, select=False)
                    self._append_status(
                        (
                            f"File is not present on disk yet: {target}. "
                            f"Opened configured download folder: {existing}"
                        ),
                        "TORRENT",
                    )
                    return

                raise FileNotFoundError(target)

            folder = self._nearest_existing_folder(
                os.path.dirname(target),
                item.save_path,
            )
            if not folder:
                raise FileNotFoundError(target)

            if sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])

            self._append_status(
                f"Opened file location: {folder}",
                "TORRENT",
            )

        except Exception as e:
            message = (
                f"Could not open file location for "
                f"{file_info.get('name') or relative_path}:\n\n"
                f"{type(e).__name__}: {e}"
            )
            self._append_status(
                message.replace("\n\n", " | "),
                "ERROR",
            )
            messagebox.showerror("Open file location", message)

    def _bind_file_row_double_click(self, row, item, file_info):
        def open_location(event=None):
            self._open_file_entry_location(item, file_info)
            return "break"

        # Bind the frame plus every visible child (labels and piece map), so a
        # double-click anywhere on the file row performs the same action.
        row.bind("<Double-1>", open_location)
        for child in row.winfo_children():
            child.bind("<Double-1>", open_location)

    def _retry_selected_failed(self):
        failed_items = [
            item
            for item in self._selected_items()
            if item.state == "error" and item.handle is None
        ]
        if not failed_items:
            return

        self.engine.use_proxy = False
        self.engine.proxy_mode = "Auto"

        for old_item in failed_items:
            retry_source = ""
            if (
                old_item.torrent_file_path
                and os.path.isfile(old_item.torrent_file_path)
            ):
                retry_source = old_item.torrent_file_path
            elif old_item.source:
                retry_source = old_item.source

            if not retry_source:
                old_item.error = "Cannot retry: torrent source is missing."
                self._append_status(
                    f"Retry unavailable for {old_item.name}: source is missing.",
                    "ERROR",
                )
                continue

            self._append_status(
                f"Manual Retry requested: {old_item.name}. "
                f"Discovery counters reset. The torrent gets up to "
                f"{self.engine.METADATA_MAX_ATTEMPTS} metadata attempts and "
                f"{self.engine.PEER_MAX_ATTEMPTS} peer attempts.",
                "TORRENT",
            )

            # Start/verify the configured network only because the user
            # explicitly requested Retry.
            if not self.engine.ensure_started(source=retry_source):
                old_item.error = (
                    "Manual Retry could not start the torrent network session."
                )
                self._persist_torrent_queue()
                continue

            # Remove only our failed queue object. No files or .torrent
            # descriptor are deleted.
            with self.engine.lock:
                self.engine.items.pop(old_item.uid, None)

            new_item = self.engine.add_torrent(
                retry_source,
                old_item.save_path,
                old_item.torrent_dir,
            )

            if new_item is not None:
                # Keep the original magnet/source identity and friendly name.
                if old_item.source:
                    new_item.source = old_item.source
                if old_item.name:
                    new_item.name = old_item.name
                if old_item.torrent_file_path:
                    new_item.torrent_file_path = old_item.torrent_file_path
                    new_item.torrent_file_saved = os.path.isfile(
                        old_item.torrent_file_path
                    )

        self._refresh_torrents()
        self._persist_torrent_queue()

    def _play_selected(self):
        uids = [item.uid for item in self._selected_items() if item.state == "paused"]
        if not uids:
            return

        self.engine.use_proxy = False
        self.engine.proxy_mode = "Auto"

        if not self.engine.ensure_started():
            return
        self.engine.resume_torrents(uids)
        self._persist_torrent_queue()

    def _pause_selected(self):
        uids = [
            item.uid
            for item in self._selected_items()
            if item.state in ("downloading", "metadata")
        ]
        if uids:
            self.engine.pause_torrents(uids)
            self._persist_torrent_queue()

    def _delete_key_remove(self, event=None):
        self._remove_selected(delete_data=False, confirm=False)
        return "break"

    def _remove_selected(self, delete_data=False, confirm=False):
        uids = self._selected_uids()
        if not uids:
            return

        if confirm:
            count = len(uids)
            noun = "torrent" if count == 1 else "torrents"
            message = (
                f"Permanently delete the selected {noun}, the saved .torrent "
                f"file{'s' if count != 1 else ''}, and all downloaded data?\n\n"
                "This cannot be undone."
            )
            if not messagebox.askyesno("Permanently delete", message):
                return

        selected_uid = self.selected_uid
        for uid in uids:
            self.engine.remove_torrent(
                uid,
                delete_data=delete_data,
                delete_descriptor=True,
            )

        if selected_uid in uids:
            self.selected_uid = None
            self._rebuild_file_view(None)

        self._persist_torrent_queue()

    def _start_all(self):
        self._append_status("Start / resume all requested.", "TORRENT")
        self.engine.use_proxy = False
        self.engine.proxy_mode = "Auto"
        if not self.engine.ensure_started():
            return

        with self.engine.lock:
            items = list(self.engine.items.values())

        for item in items:
            # Failed entries have handle=None and stay offline. Only the
            # explicit right-click Retry command may resurrect them.
            if (
                item.handle is None
                or item.state in ("finished", "error")
            ):
                continue
            try:
                item.handle.set_flags(lt.torrent_flags.auto_managed)
                item.handle.pause()
                item.handle.set_upload_mode(False)
                item.state = "queued"
                self.engine._last_progress_time[item.uid] = (
                    item.progress,
                    time.time(),
                )
            except Exception as e:
                item.state = "error"
                item.error = f"Resume failed: {type(e).__name__}: {e}"

        self._refresh_torrents()
        self._persist_torrent_queue()

    def _stop_all(self):
        self._append_status("Pause all requested.", "TORRENT")
        with self.engine.lock:
            items = list(self.engine.items.values())

        for item in items:
            if item.handle is None or item.state == "finished":
                continue
            try:
                item.handle.unset_flags(lt.torrent_flags.auto_managed)
            except Exception:
                pass
            try:
                item.handle.pause()
            except Exception:
                pass
            if item.state != "stopped":
                item.state = "paused"

        self._refresh_torrents()
        self._persist_torrent_queue()

    # --- sorting -----------------------------------------------------------
    def _sort_by(self, key):
        # Display-only operation. Sorting does not pause, resume, reload,
        # remove, or otherwise change any libtorrent handle.
        if key == "idx":
            # sorting by the index column restores insertion order; clicking
            # again reverses it
            if self.sort_key is None:
                self.sort_reverse = not self.sort_reverse
            else:
                self.sort_key = None
                self.sort_reverse = False
        elif self.sort_key == key:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_key = key
            self.sort_reverse = False
        self._refresh_torrents()
        if hasattr(self, "status_text"):
            label = {
                "idx": "#",
                "name": "Name",
                "size": "Size",
                "progress": "Progress",
                "speed": "Speed",
                "peers": "Peers",
                "status": "Status",
            }.get(key, str(key))
            direction = "descending" if self.sort_reverse else "ascending"
            self._append_status(
                f"Downloads list sorted by {label} ({direction}). "
                f"Sorting is display-only and did not change torrent state.",
                "INFO",
            )

    def _sorted_items(self):
        with self.engine.lock:
            items = list(self.engine.items.values())
        key = self.sort_key
        if key is None:
            items.sort(key=lambda it: it.uid)
        elif key == "name":
            items.sort(key=lambda it: it.name.lower())
        elif key == "speed":
            items.sort(key=lambda it: it.dl_rate)
        elif key == "progress":
            items.sort(key=lambda it: it.progress)
        elif key == "size":
            items.sort(key=lambda it: it.total)
        elif key == "peers":
            items.sort(key=lambda it: it.peers)
        elif key == "status":
            # Status is the default practical view: work that is active or
            # waiting appears first, and newer entries appear above older ones
            # within the same status. In an all-Finished list, newest is top.
            order = {
                "downloading": 0,
                "metadata": 1,
                "queued": 2,
                "paused": 3,
                "stopped": 4,
                "error": 5,
                "finished": 6,
            }
            items.sort(
                key=lambda it: (
                    order.get(it.state, 9),
                    -int(getattr(it, "uid", 0) or 0),
                )
            )
        else:
            items.sort(key=lambda it: it.uid)
        if self.sort_reverse:
            items.reverse()
        return items

    # --- torrent table refresh --------------------------------------------
    def _refresh_torrents(self):
        self._clear_progress_overlays()
        prev_selection = set()
        for row in self.tree.selection():
            uid = self._row_to_uid.get(row)
            if uid is not None:
                prev_selection.add(uid)

        for row in self.tree.get_children():
            self.tree.delete(row)
        self._row_to_uid.clear()

        items = self._sorted_items()
        active_dl = 0
        waiting_dl = 0
        fetching_metadata = 0
        total_rate = 0.0
        reselect = []
        for idx, item in enumerate(items, 1):
            pct = item.progress * 100.0
            bucket = max(0, min(100, int(round(pct / 5.0) * 5)))

            if item.state == "finished":
                row_tag = ""
                display_status = "Finished"
            elif item.state == "metadata":
                row_tag = "metadata"
                attempt = int(getattr(item, "metadata_attempt", 0) or 0)
                if attempt > 0:
                    attempt = min(
                        attempt,
                        int(self.engine.METADATA_MAX_ATTEMPTS),
                    )
                    display_status = (
                        f"Fetching metadata {attempt}/"
                        f"{self.engine.METADATA_MAX_ATTEMPTS}"
                    )
                else:
                    display_status = "Fetching metadata"
                fetching_metadata += 1
            elif item.state == "error":
                row_tag = "error"
                display_status = "Failed"
            elif item.state == "stopped":
                row_tag = "stopped"
                display_status = "Stopped"
            elif item.state == "paused":
                row_tag = "paused"
                display_status = "Paused"
            elif item.state == "queued":
                row_tag = "queued"
                display_status = "Queued"
            elif item.state == "downloading":
                row_tag = ""
                if item.dl_rate > 0:
                    display_status = "Downloading"
                elif int(getattr(item, "peer_attempt", 0) or 0) > 0:
                    attempt = min(
                        int(item.peer_attempt),
                        int(self.engine.PEER_MAX_ATTEMPTS),
                    )
                    display_status = (
                        f"Finding file payload {attempt}/"
                        f"{self.engine.PEER_MAX_ATTEMPTS}"
                    )
                    waiting_dl += 1
                elif item.peers <= 0:
                    display_status = "Waiting for peers"
                    waiting_dl += 1
                else:
                    display_status = "Waiting for data"
                    waiting_dl += 1
            else:
                row_tag = ""
                display_status = item.state.capitalize()

            # ETA uses actual file-payload speed only.
            remaining_bytes = max(
                0,
                int(getattr(item, "total", 0) or 0)
                - int(getattr(item, "downloaded", 0) or 0),
            )
            payload_speed = float(
                getattr(item, "dl_rate", 0.0) or 0.0
            )

            if item.state == "finished":
                eta_text = "00:00:00"
            elif remaining_bytes > 0 and payload_speed > 0:
                eta_text = format_eta_seconds(
                    remaining_bytes / payload_speed
                )
            else:
                eta_text = "--:--:--"

            row = self.tree.insert(
                "", "end",
                values=(
                    idx,
                    item.name,
                    human_size(item.total) if item.total else "-",
                    f"{pct:5.1f}%",
                    human_rate(item.dl_rate) if item.state == "downloading" else "-",
                    eta_text,
                    item.peers,
                    display_status,
                ),
                tags=((row_tag,) if row_tag else ()),
            )
            self._row_to_uid[row] = item.uid
            if item.uid in prev_selection:
                reselect.append(row)
            if item.state == "downloading" and item.dl_rate > 0:
                active_dl += 1
                total_rate += item.dl_rate

        if reselect:
            self.tree.selection_set(reselect)

        self.summary_var.set(
            f"{len(items)} torrent(s) | {fetching_metadata} metadata | "
            f"{active_dl} downloading | {waiting_dl} waiting | "
            f"total {human_rate(total_rate)}")

        if self.selected_uid is not None:
            self._rebuild_file_view(self.engine.items.get(self.selected_uid))

        self.after_idle(self._layout_progress_overlays)

    def _text_bar(self, frac, width=8):
        """Return a progress bar whose filled portion stays visibly green.

        Treeview can color rows but not one individual data cell. Color-emoji
        blocks keep the progress indicator green even when the row itself is
        selected, while the row background remains plain.
        """
        frac = max(0.0, min(1.0, float(frac)))
        filled = max(0, min(width, int(round(frac * width))))
        return "🟩" * filled + "▪" * (width - filled)

    # --- selection / file view --------------------------------------------
    def _on_torrent_list_click(self, event):
        """Clear selection when clicking unused list space.

        Native Treeview handling is preserved for row clicks, including
        Ctrl-click and Shift-click multi-selection.
        """
        row = self.tree.identify_row(event.y)
        region = self.tree.identify_region(event.x, event.y)

        if region == "separator":
            self._begin_column_resize(event)
            return "break"

        # Windows/Tk state bits: Shift=0x0001, Control=0x0004.
        modifying_selection = bool(event.state & (0x0001 | 0x0004))

        if (
            not row
            and region not in ("heading", "separator")
            and not modifying_selection
        ):
            current = self.tree.selection()
            if current:
                self.tree.selection_remove(current)
            self.tree.focus("")
            self.selected_uid = None
            self._rebuild_file_view(None)
            return "break"

        # Returning None lets the standard extended-selection behavior run.
        return None

    def _on_select_torrent(self, event=None):
        sel = self.tree.selection()
        if not sel:
            self.selected_uid = None
            self._rebuild_file_view(None)
            return

        uid = self._row_to_uid.get(sel[0])
        if uid is None:
            self.selected_uid = None
            self._rebuild_file_view(None)
            return

        self.selected_uid = uid
        self._rebuild_file_view(self.engine.items.get(uid))

    def _build_file_header(self):
        pad = int(6 * self.scale)
        hdr = ttk.Frame(self.file_inner)
        hdr.pack(fill="x", pady=(0, pad))
        for text, w in [("#", 4), ("File name", 40), ("Size", 12),
                        ("%", 6), ("Piece map (downloaded parts)", 40)]:
            ttk.Label(hdr, text=text, style="Header.TLabel", width=w,
                      anchor="w").pack(side="left", padx=(0, pad))
        self._file_rows.append(hdr)

    def _rebuild_file_view(self, item):
        for w in self._file_rows:
            w.destroy()
        self._file_rows = []
        if item is None or not item.files:
            lbl = ttk.Label(self.file_inner,
                            text="Select a torrent with metadata to see its files.")
            lbl.pack(anchor="w")
            self._file_rows.append(lbl)
            return

        self._build_file_header()
        pad = int(6 * self.scale)
        for i, f in enumerate(item.files, 1):
            row = ttk.Frame(self.file_inner)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=str(i), width=4, anchor="w").pack(
                side="left", padx=(0, pad))
            ttk.Label(row, text=f["name"], width=40, anchor="w").pack(
                side="left", padx=(0, pad))
            ttk.Label(row, text=human_size(f["size"]), width=12,
                      anchor="w").pack(side="left", padx=(0, pad))
            ttk.Label(row, text=f"{f['progress']*100:4.0f}%", width=6,
                      anchor="w").pack(side="left", padx=(0, pad))
            bar = PieceMapBar(row, height=int(18 * self.scale),
                              width=int(320 * self.scale))
            bar.pack(side="left", fill="x", expand=True)
            fp, lp = f["first_piece"], f["last_piece"]
            seg = item.piece_map[fp:lp + 1] if (item.piece_map and lp >= fp) else []
            bar.set_pieces(seg)
            self._bind_file_row_double_click(row, item, f)
            self._file_rows.append(row)

    # --- proxy table -------------------------------------------------------
    def _refresh_proxy_table(self):
        for row in self.ptree.get_children():
            self.ptree.delete(row)
        for key, proxy_type, status, active in self.proxy_mgr.snapshot():
            if status in ("failed", "torrent-blocked"):
                tag = "failed"
            elif status in ("testing", "torrent-testing", "transport-ok"):
                tag = "testing"
            elif active and status == "torrent-ok":
                tag = "active"
            else:
                tag = ""
            self.ptree.insert(
                "",
                "end",
                values=(
                    key,
                    proxy_type or "-",
                    status,
                    "YES" if active else "",
                ),
                tags=(tag,),
            )

    # --- event pump --------------------------------------------------------
    def _poll_events(self):
        try:
            while True:
                kind, payload = self.event_q.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self.after(400, self._poll_events)

    def _handle_event(self, kind, payload):
        if kind == "torrents":
            self._refresh_torrents()

        elif kind == "proxy":
            self._refresh_proxy_table()

        elif kind == "proxy_connection":
            data = payload or {}
            self._set_proxy_indicator(
                connected=bool(data.get("connected")),
                proxy=str(data.get("proxy") or ""),
                proxy_type=str(data.get("type") or ""),
                detail=str(data.get("detail") or ""),
                disabled=bool(data.get("disabled")),
                testing=bool(data.get("testing")),
                unverified=bool(data.get("unverified")),
            )
            detail = str(data.get("detail") or "")
            proxy = str(data.get("proxy") or "")
            proxy_type = str(data.get("type") or "")
            if data.get("disabled"):
                message = ""
                self._append_proxy_detail(message)
                self._append_status(message, "PROXY")
            elif data.get("connected"):
                message = (
                    f"BITTORRENT VERIFIED proxy: {proxy} "
                    f"{f'({proxy_type})' if proxy_type else ''}. {detail}"
                )
                self._append_proxy_detail(message)
                self._append_status(message, "OK")
            elif data.get("unverified"):
                message = (
                    f"TRANSPORT ONLY — torrent unverified: {proxy} "
                    f"{f'({proxy_type})' if proxy_type else ''}. {detail}"
                )
                self._append_proxy_detail(message)
                self._append_status(message, "PROXY")
            elif data.get("testing"):
                message = f"Testing proxy {proxy or 'list'}... {detail}"
                self._append_proxy_detail(message)
                self._append_status(message, "PROXY")
            else:
                message = f"Proxy not connected: {proxy or 'none'}. {detail}"
                self._append_proxy_detail(message)
                self._append_status(message, "PROXY")
            self._refresh_proxy_table()

        elif kind == "queue_changed":
            self._persist_torrent_queue()

        elif kind == "duplicate_torrent":
            data = payload or {}
            uid = data.get("uid")
            if uid is not None:
                self._select_torrent_uid(uid)
            else:
                self._refresh_torrents()

        elif kind == "status":
            self._append_status(str(payload), "INFO")

        elif kind == "status_detail":
            data = payload or {}
            message = str(data.get("message") or "")
            level = str(data.get("level") or "INFO")
            if level == "PROXY":
                self._append_proxy_detail(message)
            self._append_status(message, level)

        elif kind == "torrent_diagnostic":
            data = payload or {}
            tracker = str(data.get("tracker") or "none")
            if len(tracker) > 110:
                tracker = tracker[:107] + "..."
            proxy = str(data.get("proxy") or "none")
            ptype = str(data.get("proxy_type") or "")
            proxy_text = proxy + (f" ({ptype})" if ptype else "")
            state = str(data.get("state") or "unknown")
            peers = int(data.get("peers") or 0)
            seeds = int(data.get("seeds") or 0)
            raw_rate = float(data.get("rate") or 0)
            raw_network_rate = float(data.get("network_rate") or 0)
            payload_total = int(data.get("payload_total") or 0)
            rate = human_rate(raw_rate)
            network_rate = human_rate(raw_network_rate)
            metadata = "yes" if data.get("metadata") else "no"
            dht = "on" if data.get("dht") else "off"
            dht_nodes = int(data.get("dht_nodes", -1) or -1)

            visible_state = state
            explanation = ""
            if state == "metadata":
                visible_state = "FETCHING METADATA"
                explanation = " | waiting for torrent metadata from peers"
            elif state == "downloading" and peers <= 0:
                visible_state = "WAITING FOR PEERS"
                explanation = " | no connected peers are currently available"
            elif state == "downloading" and raw_rate <= 0:
                visible_state = "WAITING FOR DATA"
                if raw_network_rate > 0:
                    explanation = (
                        f" | no file payload; {network_rate} is only "
                        f"BitTorrent/network protocol traffic"
                    )
                else:
                    explanation = (
                        " | peer connection(s) exist, but none are sending "
                        "file payload"
                    )
            elif state == "downloading":
                visible_state = "DOWNLOADING"

            self._append_status(
                (
                    f"{data.get('name', 'Torrent')}: {visible_state} | "
                    f"metadata={metadata} | peers={peers} | seeds={seeds} | "
                    f"payload rate={rate} | session payload="
                    f"{human_size(payload_total)} | DHT={dht}"
                    + (
                        f" ({dht_nodes} nodes)"
                        if dht_nodes >= 0
                        else ""
                    )
                    + explanation
                ),
                "TORRENT",
            )

        elif kind == "engine_alert":
            data = payload or {}
            what = str(data.get("what") or "libtorrent")
            message = str(data.get("message") or "")
            level = "ERROR" if "error" in what.lower() or "failed" in what.lower() else "TORRENT"
            self._append_status(f"libtorrent {what}: {message}", level)

        elif kind == "error":
            self._append_status(f"ERROR: {payload}", "ERROR")
            self._refresh_proxy_table()
            messagebox.showerror("Notice", str(payload))

    def _on_close(self):
        # Title-bar X is intentionally not an exit command.
        self._hide_to_tray()


    def _autoload_source(self, source):
        """Safely queue a browser/.torrent argument on the first app instance."""
        if not source:
            return
        try:
            self.after(
                450,
                lambda s=source: self._enqueue_external_source(
                    s,
                    origin="startup browser link",
                ),
            )
        except Exception as e:
            log_external_error(
                "Could not schedule startup external source.",
                exc=e,
                details=f"source={source!r}",
            )


def run_libtorrent_self_test():
    """Installer smoke-test for the exact frozen libtorrent runtime."""
    try:
        log_startup_diagnostic(
            "self-test: constructing libtorrent with safe startup settings"
        )
        session = lt.session(safe_libtorrent_startup_settings())
        log_startup_diagnostic(
            "self-test: safe libtorrent session constructed"
        )
        session.apply_settings(
            compatible_libtorrent_settings(5)
        )
        log_startup_diagnostic(
            "self-test: compatibility settings applied"
        )
        # A successful constructor plus settings application is the runtime
        # path the app needs. Destroy the Python object normally so the native
        # libtorrent session also proves it can shut down cleanly.
        del session
        log_startup_diagnostic(
            "self-test: libtorrent session destroyed cleanly"
        )
        return 0
    except Exception as e:
        log_external_error(
            "Libtorrent self-test raised a Python exception.",
            exc=e,
        )
        return 41


def main():
    if "--self-test-libtorrent" in sys.argv[1:]:
        raise SystemExit(run_libtorrent_self_test())

    log_startup_diagnostic("process launch entered main()")
    source = command_line_source()
    instance_started_ns = time.time_ns()
    log_startup_diagnostic(
        "command-line source parsed"
        + (f": {source[:240]}" if source else ": <none>")
    )

    if not acquire_single_instance():
        log_startup_diagnostic(
            "another instance already owns the mutex; entering forwarding path"
        )
        if source:
            try:
                forward_source_to_running_instance(source)
                log_startup_diagnostic(
                    "external source forwarded successfully to running instance"
                )
            except Exception as e:
                log_path = log_external_error(
                    "Failed to forward browser link to running instance.",
                    exc=e,
                    details=f"source={source!r}",
                )
                try:
                    import tkinter as _tk
                    from tkinter import messagebox as _mb
                    _r = _tk.Tk()
                    _r.withdraw()
                    _mb.showerror(
                        "GetTorrent",
                        (
                            "GetTorrent is already running, but the "
                            "browser link could not be forwarded."
                            "\n\n"
                            f"{type(e).__name__}: {e}"
                            + (
                                f"\n\nDiagnostic log:\n{log_path}"
                                if log_path
                                else ""
                            )
                        ),
                    )
                    _r.destroy()
                except Exception:
                    pass
        return

    try:
        try:
            clear_stale_ipc_messages(instance_started_ns)
        except Exception as e:
            log_external_error(
                "Could not clear stale IPC messages.",
                exc=e,
            )

        scale = enable_high_dpi()
        log_startup_diagnostic("creating primary GUI instance")
        app = App(scale)
        log_startup_diagnostic("primary GUI instance created")

        if source:
            app._autoload_source(source)

        app.mainloop()
    finally:
        release_single_instance()


def _run_with_crash_log():
    """Wrap main() so that, in a --noconsole build, any startup exception
    (e.g. while handling a magnet: or .torrent argument) is written to a log
    and shown in a dialog instead of the process silently vanishing."""
    import traceback
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        # log next to the user's temp dir
        try:
            log_dir = app_data_folder()
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "TorrentDownloader-crash.txt")
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("GetTorrent crashed on startup.\n\n")
                f.write("Arguments: %r\n\n" % (sys.argv,))
                f.write(tb)
        except Exception:
            log_path = "(could not write log)"
        # try to show a dialog even if the main window never came up
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb
            _r = _tk.Tk()
            _r.withdraw()
            _mb.showerror(
                "GetTorrent - startup error",
                "The app failed to start.\n\n%s\n\nDetails saved to:\n%s"
                % (tb.strip().splitlines()[-1] if tb.strip() else "unknown",
                   log_path))
            _r.destroy()
        except Exception:
            pass
        return


if __name__ == "__main__":
    _run_with_crash_log()
