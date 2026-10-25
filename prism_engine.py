"""Prism media engine.

Everything that touches pixels, FFmpeg, hardware or the history database lives
here, with no GUI dependencies. Image worker processes import only this module,
so top-level imports stay light (PyAV and NumPy are imported lazily).
"""
from __future__ import annotations

import io
import os
import re
import sys
import time
import uuid
import shutil
import signal
import struct
import sqlite3
import hashlib
import platform
import threading
import traceback
import subprocess
import multiprocessing as mp
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from concurrent.futures import (
    ProcessPoolExecutor, ThreadPoolExecutor, CancelledError,
    as_completed, wait, FIRST_COMPLETED,
)
from concurrent.futures.process import BrokenProcessPool
from typing import Callable, Optional

from PIL import Image, ImageOps, ImageSequence, ImageStat, ImageFile, UnidentifiedImageError

try:
    from PIL import ImageCms
except ImportError:  # Pillow built without littlecms
    ImageCms = None

try:
    import pillow_jxl  # noqa: F401  (registers the JXL codec with Pillow)
    HAS_JXL = True
except ImportError:
    HAS_JXL = False

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HAS_HEIF = True
except ImportError:
    HAS_HEIF = False

# A truncated file must fail loudly, never be "compressed" with grey padding.
ImageFile.LOAD_TRUNCATED_IMAGES = False
Image.MAX_IMAGE_PIXELS = 1_000_000_000

APP_NAME = "Prism"
APP_VERSION = "3.0"

IS_WIN = sys.platform == "win32"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)

# RAW formats are deliberately absent: Pillow only decodes their embedded preview.
IMAGE_EXTS = frozenset({
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".bmp", ".gif", ".tif", ".tiff",
    ".webp", ".heic", ".heif", ".avif", ".jxl",
})
VIDEO_EXTS = frozenset({
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm", ".mts", ".m2ts", ".ts",
    ".m4v", ".wmv", ".3gp", ".mpg", ".mpeg",
})
MIN_IMAGE_BYTES = 5 * 1024
MIN_VIDEO_BYTES = 50 * 1024
TEMP_PREFIX = "~prism_"
TEMP_RE = re.compile(r"^~prism_[0-9a-f]{32}\.tmp(\.[A-Za-z0-9]+)?$")
SKIP_DIRS = frozenset({"PROCESSED", "FAILED"})
WEBP_MAX_DIM = 16383

LogFn = Callable[[str, str], None]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def app_data_dir() -> str:
    if IS_WIN:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def fmt_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _run(cmd: list, timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW)


def _remove_quietly(path: Optional[str]):
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def _copy_times(src: str, dst: str):
    try:
        st = os.stat(src)
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Process control (pause / resume / cancel)
# ---------------------------------------------------------------------------

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _ntdll.NtSuspendProcess.argtypes = (wintypes.HANDLE,)
    _ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    _PROCESS_SUSPEND_RESUME = 0x0800

    def _signal_pid(pid: int, suspend: bool):
        handle = _k32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, pid)
        if handle:
            try:
                (_ntdll.NtSuspendProcess if suspend else _ntdll.NtResumeProcess)(handle)
            finally:
                _k32.CloseHandle(handle)
else:
    def _signal_pid(pid: int, suspend: bool):
        try:
            os.kill(pid, signal.SIGSTOP if suspend else signal.SIGCONT)
        except OSError:
            pass


def _lower_own_priority():
    try:
        if IS_WIN:
            _k32.SetPriorityClass(_k32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL
        else:
            os.nice(5)
    except Exception:
        pass


class Control:
    """Pause/cancel plumbing shared by video threads, FFmpeg children and image workers.

    Pausing suspends every registered OS process, so even a frame that is
    mid-encode stops instantly instead of at the next checkpoint.
    """

    def __init__(self):
        ctx = mp.get_context("spawn")
        self.cancel_event = threading.Event()
        self.running_event = threading.Event()
        self.running_event.set()
        self.mp_cancel = ctx.Event()
        self.mp_running = ctx.Event()
        self.mp_running.set()
        self._procs: set = set()
        self._worker_pids: set = set()
        self._lock = threading.Lock()
        self.paused = False

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def wait_running(self):
        while not self.running_event.wait(0.25):
            if self.cancelled:
                return

    def register(self, proc: subprocess.Popen):
        with self._lock:
            self._procs.add(proc)
            if self.paused:
                _signal_pid(proc.pid, True)

    def unregister(self, proc: subprocess.Popen):
        with self._lock:
            self._procs.discard(proc)

    def set_worker_pids(self, pids):
        with self._lock:
            new = set(pids) - self._worker_pids
            self._worker_pids = set(pids)
            if self.paused:
                for pid in new:
                    _signal_pid(pid, True)

    def _all_pids(self):
        return [p.pid for p in self._procs] + list(self._worker_pids)

    def pause(self):
        with self._lock:
            if self.paused or self.cancelled:
                return
            self.paused = True
            self.running_event.clear()
            self.mp_running.clear()
            for pid in self._all_pids():
                _signal_pid(pid, True)

    def resume(self):
        with self._lock:
            if not self.paused:
                return
            self.paused = False
            for pid in self._all_pids():
                _signal_pid(pid, False)
            self.running_event.set()
            self.mp_running.set()

    def cancel(self):
        self.cancel_event.set()
        self.mp_cancel.set()
        self.resume()
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            try:
                proc.kill()
            except Exception:
                pass


# Set inside image worker processes by the pool initializer.
_W_CANCEL = None
_W_RUNNING = None


def _worker_init(cancel_event, running_event):
    global _W_CANCEL, _W_RUNNING
    _W_CANCEL, _W_RUNNING = cancel_event, running_event
    _lower_own_priority()


def _worker_cancelled() -> bool:
    return _W_CANCEL is not None and _W_CANCEL.is_set()


def _worker_wait_running():
    if _W_RUNNING is not None:
        while not _W_RUNNING.wait(0.25):
            if _worker_cancelled():
                return


# ---------------------------------------------------------------------------
# Fingerprints & history database
# ---------------------------------------------------------------------------

def file_fingerprint(path: str) -> str:
    """Content fingerprint used as the history key.

    Must stay byte-compatible with Prism 2.x so existing history keeps matching:
    SHA-1 of the whole file up to 50 MB, otherwise SHA-1 of three 2 MB samples
    (start, middle, end) followed by the decimal file size.
    """
    sha1 = hashlib.sha1()
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > 50 * 1024 * 1024:
            chunk = 2 * 1024 * 1024
            sha1.update(f.read(chunk))
            if size > chunk * 2:
                f.seek(size // 2)
                sha1.update(f.read(chunk))
            if size > chunk:
                f.seek(-chunk, 2)
                sha1.update(f.read(chunk))
            sha1.update(str(size).encode("utf-8"))
        else:
            while data := f.read(1024 * 1024):
                sha1.update(data)
    return sha1.hexdigest()


class History:
    """SQLite history: finished outputs, cached fingerprints and 'already optimal' verdicts.

    One shared connection guarded by a lock; only the session/watch threads write.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._conn:
            self._conn.execute("CREATE TABLE IF NOT EXISTS processed_files (file_hash TEXT PRIMARY KEY, output_path TEXT NOT NULL, timestamp TEXT NOT NULL)")
            self._conn.execute("CREATE TABLE IF NOT EXISTS path_cache (path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, file_hash TEXT NOT NULL)")
            self._conn.execute("CREATE TABLE IF NOT EXISTS verdicts (file_hash TEXT NOT NULL, settings_key TEXT NOT NULL, timestamp TEXT NOT NULL, PRIMARY KEY (file_hash, settings_key))")

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S")

    def load_fingerprint_cache(self) -> dict:
        with self._lock:
            rows = self._conn.execute("SELECT path, size, mtime_ns, file_hash FROM path_cache").fetchall()
        return {r[0]: (r[1], r[2], r[3]) for r in rows}

    def store_fingerprints(self, rows):
        if not rows:
            return
        with self._lock, self._conn:
            self._conn.executemany("INSERT OR REPLACE INTO path_cache (path, size, mtime_ns, file_hash) VALUES (?, ?, ?, ?)", rows)

    def processed_output(self, fp: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT output_path FROM processed_files WHERE file_hash = ?", (fp,)).fetchone()
        return row[0] if row else None

    def mark_processed(self, fp: str, output_path: str):
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO processed_files (file_hash, output_path, timestamp) VALUES (?, ?, ?)", (fp, output_path, self._now()))

    def has_verdict(self, fp: str, key: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM verdicts WHERE file_hash = ? AND settings_key = ?", (fp, key)).fetchone() is not None

    def mark_verdict(self, fp: str, key: str):
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO verdicts (file_hash, settings_key, timestamp) VALUES (?, ?, ?)", (fp, key, self._now()))

    def clear(self) -> bool:
        try:
            with self._lock:
                with self._conn:
                    self._conn.execute("DELETE FROM processed_files")
                    self._conn.execute("DELETE FROM verdicts")
                self._conn.execute("VACUUM")
            return True
        except sqlite3.Error:
            return False

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


# ---------------------------------------------------------------------------
# Hardware & FFmpeg discovery
# ---------------------------------------------------------------------------

@dataclass
class Encoder:
    name: str
    family: str      # av1 | hevc | h264
    vendor: str      # nvidia | amd | intel | apple | cpu
    label: str
    tier: int = 0    # index into _encoder_tiers() that passed the self-test
    ten_bit: bool = False

    @property
    def is_gpu(self) -> bool:
        return self.vendor != "cpu"

    @property
    def pix_fmt(self) -> str:
        if self.ten_bit:
            return "p010le" if self.is_gpu else "yuv420p10le"
        return "nv12" if self.is_gpu else "yuv420p"


ENCODER_CANDIDATES = [
    ("av1_nvenc", "av1", "nvidia", "NVIDIA AV1 · NVENC"),
    ("hevc_nvenc", "hevc", "nvidia", "NVIDIA HEVC · NVENC"),
    ("h264_nvenc", "h264", "nvidia", "NVIDIA H.264 · NVENC"),
    ("av1_amf", "av1", "amd", "AMD AV1 · AMF"),
    ("hevc_amf", "hevc", "amd", "AMD HEVC · AMF"),
    ("h264_amf", "h264", "amd", "AMD H.264 · AMF"),
    ("av1_qsv", "av1", "intel", "Intel AV1 · Quick Sync"),
    ("hevc_qsv", "hevc", "intel", "Intel HEVC · Quick Sync"),
    ("h264_qsv", "h264", "intel", "Intel H.264 · Quick Sync"),
    ("hevc_videotoolbox", "hevc", "apple", "Apple HEVC · VideoToolbox"),
    ("h264_videotoolbox", "h264", "apple", "Apple H.264 · VideoToolbox"),
    ("libsvtav1", "av1", "cpu", "CPU AV1 · SVT-AV1"),
    ("libx265", "hevc", "cpu", "CPU HEVC · x265"),
    ("libx264", "h264", "cpu", "CPU H.264 · x264"),
]

# Codecs NVDEC can decode straight into VRAM for the zero-copy path.
NVDEC_CODECS = frozenset({"h264", "hevc", "av1", "vp9", "vp8", "mpeg2video", "mpeg1video", "mpeg4", "vc1"})
NVDEC_PIXFMTS = frozenset({"yuv420p", "yuvj420p", "nv12", "yuv420p10le", "p010le"})


def _family_quality(family: str, q: int) -> int:
    """Map the HEVC-equivalent quality slider onto each codec's native scale."""
    if family == "av1":
        return min(63, round(q * 63 / 51))
    if family == "h264":
        return max(0, q - 4)
    return q


def _encoder_tiers(name: str, family: str, q: int) -> list:
    """Option sets from richest to most conservative; detection picks the first that works."""
    fq = _family_quality(family, q)
    if name.endswith("_nvenc"):
        base = ["-rc", "vbr", "-cq", str(fq), "-b:v", "0"]
        return [
            ["-preset", "p7", "-tune", "hq", *base, "-multipass", "qres", "-spatial-aq", "1",
             "-temporal-aq", "1", "-aq-strength", "8", "-rc-lookahead", "32", "-b_ref_mode", "middle"],
            ["-preset", "p6", "-tune", "hq", *base, "-spatial-aq", "1", "-rc-lookahead", "20"],
            base,
        ]
    if name.endswith("_amf"):
        qp = round(fq * 255 / 63) if family == "av1" else fq
        cqp = ["-rc", "cqp", "-qp_i", str(qp), "-qp_p", str(qp)]
        return [
            ["-quality", "quality", *cqp, *([] if family == "av1" else ["-qp_b", str(min(51, qp + 2))])],
            cqp,
        ]
    if name.endswith("_qsv"):
        return [
            ["-preset", "veryslow", "-global_quality", str(q)],
            ["-global_quality", str(q)],
        ]
    if name.endswith("_videotoolbox"):
        # -q:v runs 1-100 (higher is better); constant quality needs Apple Silicon.
        qv = max(1, min(100, round(100 - fq * 1.6)))
        return [["-q:v", str(qv), "-allow_sw", "0"], ["-q:v", str(qv)]]
    if name == "libsvtav1":
        return [["-preset", "6", "-crf", str(fq), "-svtav1-params", "tune=0"], ["-crf", str(fq)]]
    if name == "libx265":
        return [["-preset", "medium", "-crf", str(fq), "-x265-params", "log-level=error"], ["-crf", str(fq)]]
    return [["-preset", "slow", "-crf", str(fq)], ["-crf", str(fq)]]


@dataclass
class Hardware:
    ffmpeg: Optional[str] = None
    ffmpeg_version: str = ""
    encoders: list = field(default_factory=list)
    cuda: bool = False
    cuda_deint: bool = False
    opus: bool = False
    gpu_name: str = ""
    gpu_driver: str = ""
    vram_gb: float = 0.0
    cpu_name: str = ""
    cpu_threads: int = 0
    ram_total_gb: float = 0.0
    ram_free_gb: float = 0.0

    def chain(self, pref: str = "auto") -> list:
        """Encoders to try for a codec preference, best first."""
        order = {"auto": ["av1", "hevc", "h264"], "av1": ["av1", "hevc", "h264"],
                 "hevc": ["hevc", "av1", "h264"], "h264": ["h264", "hevc", "av1"]}.get(pref, ["av1", "hevc", "h264"])
        rank = {fam: i for i, fam in enumerate(order)}
        if pref == "auto":
            key = lambda e: (not e.is_gpu, rank[e.family])
        else:
            key = lambda e: (e.family != pref, not e.is_gpu, rank[e.family])
        return sorted(self.encoders, key=key)

    @property
    def primary(self) -> Optional[Encoder]:
        chain = self.chain()
        return chain[0] if chain else None


def _ffmpeg_works(path: str) -> bool:
    try:
        return os.path.isfile(path) and _run([path, "-hide_banner", "-version"], timeout=6).returncode == 0
    except Exception:
        return False


def find_ffmpeg() -> Optional[str]:
    exe = "ffmpeg.exe" if IS_WIN else "ffmpeg"
    here = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
    candidates = [
        os.path.join(here, exe),
        os.path.join(getattr(sys, "_MEIPASS", here), exe),
        shutil.which("ffmpeg"),
    ]
    if IS_WIN:
        local = os.environ.get("LOCALAPPDATA", "")
        home = os.environ.get("USERPROFILE", "")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        candidates += [
            os.path.join(local, "Microsoft", "WinGet", "Links", exe),
            os.path.join(home, "scoop", "shims", exe),
            r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
            r"C:\ffmpeg\bin\ffmpeg.exe",
            os.path.join(pf, "ffmpeg", "bin", exe),
        ]
    candidates.append(os.path.join(app_data_dir(), "bin", exe))
    seen = set()
    for c in candidates:
        if not c:
            continue
        key = os.path.normcase(os.path.abspath(c))
        if key in seen:
            continue
        seen.add(key)
        if _ffmpeg_works(c):
            return c
    try:
        import imageio_ffmpeg
        c = imageio_ffmpeg.get_ffmpeg_exe()
        if c and _ffmpeg_works(c):
            return c
    except Exception:
        pass
    return None


def download_ffmpeg(log: Optional[LogFn] = None) -> Optional[str]:
    """Fetch a static FFmpeg build into Prism's own folder.

    Windows only; on macOS and Linux the bundled imageio-ffmpeg binary or the
    system package (brew / apt / dnf) is used instead.
    """
    if not IS_WIN:
        return None
    import urllib.request
    import zipfile
    dest_dir = os.path.join(app_data_dir(), "bin")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "ffmpeg.exe")
    for url in ("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
                "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"):
        zip_path = os.path.join(dest_dir, f"{TEMP_PREFIX}{uuid.uuid4().hex}.tmp.zip")
        try:
            if log:
                log("info", f"Downloading FFmpeg from {url.split('/')[2]}…")
            req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(zip_path, "wb") as out:
                shutil.copyfileobj(resp, out, 1024 * 1024)
            with zipfile.ZipFile(zip_path) as zf:
                member = next((m for m in zf.namelist() if m.lower().endswith("bin/ffmpeg.exe")), None)
                if member:
                    tmp = dest + ".part"
                    with zf.open(member) as src, open(tmp, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    os.replace(tmp, dest)
            if _ffmpeg_works(dest):
                return dest
        except Exception as e:
            if log:
                log("warn", f"FFmpeg download failed: {e}")
        finally:
            _remove_quietly(zip_path)
    return None


def _test_encoder(ffmpeg: str, name: str, family: str, vendor: str, label: str) -> Optional[Encoder]:
    tiers = _encoder_tiers(name, family, 28)
    depths = [True, False] if family != "h264" else [False]
    for ten_bit in depths:
        enc = Encoder(name, family, vendor, label, 0, ten_bit)
        for tier, args in enumerate(tiers):
            cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-f", "lavfi",
                   "-i", "testsrc2=s=640x360:r=30:d=0.5", "-vf", f"format={enc.pix_fmt}",
                   "-c:v", name, *args, "-f", "null", "-"]
            try:
                if _run(cmd, timeout=30).returncode == 0:
                    enc.tier = tier
                    return enc
            except Exception:
                pass
    return None


def _cpu_name() -> str:
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            return _run(["sysctl", "-n", "machdep.cpu.brand_string"], 3).stdout.strip()
        except Exception:
            pass
    else:
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except Exception:
            pass
    return platform.processor() or "CPU"


def _ram_gb() -> tuple:
    if IS_WIN:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return stat.ullTotalPhys / 2**30, stat.ullAvailPhys / 2**30
    total = avail = 0.0
    try:
        page = os.sysconf("SC_PAGE_SIZE")
        total = page * os.sysconf("SC_PHYS_PAGES") / 2**30
        avail = page * os.sysconf("SC_AVPHYS_PAGES") / 2**30
    except (ValueError, OSError, AttributeError):
        pass
    if sys.platform == "darwin" and not total:
        try:
            total = int(_run(["sysctl", "-n", "hw.memsize"], 3).stdout.strip()) / 2**30
        except Exception:
            pass
    return total, avail


def _gpu_info(hw: Hardware):
    try:
        r = _run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"], 5)
        if r.returncode == 0 and r.stdout.strip():
            name, driver, mem = [p.strip() for p in r.stdout.strip().splitlines()[0].split(",")[:3]]
            hw.gpu_name, hw.gpu_driver, hw.vram_gb = name, driver, float(mem) / 1024
            return
    except Exception:
        pass
    try:
        if IS_WIN:
            r = _run(["powershell", "-NoProfile", "-Command",
                      "(Get-CimInstance Win32_VideoController | Select-Object -First 1).Name"], 8)
            hw.gpu_name = r.stdout.strip()
        elif sys.platform == "darwin":
            r = _run(["system_profiler", "SPDisplaysDataType"], 10)
            m = re.search(r"Chipset Model:\s*(.+)", r.stdout)
            hw.gpu_name = m.group(1).strip() if m else ""
        else:
            r = _run(["lspci"], 5)
            m = re.search(r"(?:VGA compatible controller|3D controller|Display controller):\s*(.+)", r.stdout)
            hw.gpu_name = re.sub(r"\s*\(rev \w+\)$", "", m.group(1).strip()) if m else ""
    except Exception:
        pass


def detect_hardware(log: Optional[LogFn] = None, allow_download: bool = True) -> Hardware:
    hw = Hardware(cpu_name=_cpu_name(), cpu_threads=os.cpu_count() or 4)
    hw.ram_total_gb, hw.ram_free_gb = _ram_gb()
    gpu_thread = threading.Thread(target=_gpu_info, args=(hw,), daemon=True)
    gpu_thread.start()

    hw.ffmpeg = find_ffmpeg()
    if not hw.ffmpeg and allow_download:
        hw.ffmpeg = download_ffmpeg(log)
    if hw.ffmpeg:
        try:
            first = _run([hw.ffmpeg, "-hide_banner", "-version"], 6).stdout.splitlines()[0]
            hw.ffmpeg_version = first.split(" version ")[1].split(" ")[0] if " version " in first else first
            listed = _run([hw.ffmpeg, "-hide_banner", "-encoders"], 10).stdout
            filters = _run([hw.ffmpeg, "-hide_banner", "-filters"], 10).stdout
        except Exception:
            listed = filters = ""
        hw.opus = " libopus " in listed
        present = [c for c in ENCODER_CANDIDATES if re.search(rf"\s{c[0]}\s", listed)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda c: _test_encoder(hw.ffmpeg, *c), present))
        hw.encoders = [e for e in results if e]
        if any(e.vendor == "nvidia" for e in hw.encoders) and " scale_cuda " in filters:
            nv = next(e for e in hw.encoders if e.vendor == "nvidia")
            cmd = [hw.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-init_hw_device", "cuda=cu",
                   "-filter_hw_device", "cu", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=0.3",
                   "-vf", f"hwupload_cuda,scale_cuda=format={nv.pix_fmt}", "-c:v", nv.name, "-f", "null", "-"]
            try:
                hw.cuda = _run(cmd, 30).returncode == 0
            except Exception:
                hw.cuda = False
            hw.cuda_deint = hw.cuda and " bwdif_cuda " in filters
    gpu_thread.join(10)
    return hw


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    input_dir: str = ""
    output_dir: str = ""
    images: bool = True
    videos: bool = True
    img_format: str = "webp"      # original | webp | jpeg | jxl
    img_quality: int = 75
    img_max_dim: int = 0          # longest edge in px, 0 = keep
    quality_guard: bool = True
    vid_codec: str = "auto"       # auto | av1 | hevc | h264
    vid_quality: int = 28         # HEVC-equivalent CQ, lower = better
    vid_max_height: int = 0       # 0 = keep, else 2160 / 1440 / 1080 / 720
    vid_fps_cap: bool = False
    vid_jobs: int = 2
    preserve_metadata: bool = True
    scan_subfolders: bool = True
    append_hash: bool = True
    copy_on_skip: bool = False
    move_processed: bool = False
    move_failed: bool = True
    overwrite: bool = False

    def image_key(self) -> str:
        return f"img|{self.img_format}|{self.img_quality}|{self.img_max_dim}|{int(self.quality_guard)}|{int(self.preserve_metadata)}"

    def video_key(self, hw: Hardware) -> str:
        chain = hw.chain(self.vid_codec)
        enc = chain[0].name if chain else "none"
        return f"vid|{enc}|{self.vid_quality}|{self.vid_max_height}|{int(self.vid_fps_cap)}|{int(self.preserve_metadata)}"


def _new_result(job: dict) -> dict:
    return {"status": "error", "reason": "", "message": "", "out_path": None,
            "in_size": job.get("size", 0), "out_size": 0, "log": []}


class _Skip(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def _exif_reset_orientation(raw: Optional[bytes]) -> Optional[bytes]:
    """Set EXIF Orientation to 1 in place, leaving every other byte untouched.

    A decode/re-encode round-trip would move data and break MakerNote offsets.
    """
    if not raw:
        return raw
    prefix = b"Exif\x00\x00" if raw.startswith(b"Exif\x00\x00") else b""
    tiff = bytearray(raw[len(prefix):])
    if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        return raw
    e = "<" if tiff[:2] == b"II" else ">"
    try:
        ifd = struct.unpack_from(e + "I", tiff, 4)[0]
        count = struct.unpack_from(e + "H", tiff, ifd)[0]
        for i in range(count):
            off = ifd + 2 + i * 12
            tag, typ, cnt = struct.unpack_from(e + "HHI", tiff, off)
            if tag == 0x0112 and typ == 3 and cnt == 1:
                struct.pack_into(e + "H", tiff, off + 8, 1)
                break
    except struct.error:
        return raw
    return prefix + bytes(tiff)


def _xmp_reset_orientation(xmp: Optional[bytes]) -> Optional[bytes]:
    if not xmp:
        return xmp
    xmp = re.sub(rb'(tiff:Orientation\s*=\s*["\'])\d(["\'])', rb"\g<1>1\g<2>", xmp)
    return re.sub(rb"(<tiff:Orientation>)\s*\d\s*(</tiff:Orientation>)", rb"\g<1>1\g<2>", xmp)


def _capture_metadata(im: Image.Image) -> dict:
    exif = im.info.get("exif")
    if isinstance(exif, Image.Exif):
        exif = exif.tobytes()
    if not exif:
        try:
            e = im.getexif()
            exif = e.tobytes() if len(e) else None
        except Exception:
            exif = None
    xmp = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp")
    if isinstance(xmp, str):
        xmp = xmp.encode("utf-8")
    return {"exif": exif or None, "xmp": xmp or None, "icc": im.info.get("icc_profile") or None}


def _is_srgb(icc: Optional[bytes]) -> bool:
    if not icc or ImageCms is None:
        return True
    try:
        desc = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))) or ""
        return "srgb" in desc.lower()
    except Exception:
        return True


def _to_srgb(img: Image.Image, icc: Optional[bytes]) -> tuple:
    """Convert pixels into sRGB so a format without ICC support keeps correct colours."""
    if ImageCms is None or not icc or _is_srgb(icc) or img.mode not in ("RGB", "RGBA"):
        return img, icc
    try:
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        out = ImageCms.profileToProfile(img, src, ImageCms.createProfile("sRGB"), outputMode=img.mode)
        return out, None
    except Exception:
        return img, icc


def _wide_gray_to_l(img: Image.Image) -> Image.Image:
    """16-bit / float greyscale -> 8-bit by scaling (Pillow's plain convert clips to white)."""
    if img.mode.startswith("I;16"):
        img = img.convert("I")
    lo, hi = img.getextrema()
    if img.mode == "F":
        scale = 255.0 if hi <= 1.0 else (255.0 / hi if hi > 255 else 1.0)
    else:
        scale = 1 / 257 if hi > 255 else 1.0
    if scale != 1.0:
        img = img.point(lambda v: v * scale)
    return img.convert("L")


def _normalize(img: Image.Image, icc: Optional[bytes]) -> tuple:
    """Bring any Pillow mode into L / LA / RGB / RGBA. Returns (image, icc, has_alpha)."""
    mode = img.mode
    if mode in ("I", "F") or mode.startswith("I;16"):
        img = _wide_gray_to_l(img)
    elif mode == "1":
        img = img.convert("L")
    elif mode == "CMYK":
        converted = None
        if ImageCms is not None and icc:
            try:
                converted = ImageCms.profileToProfile(img, ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                                      ImageCms.createProfile("sRGB"), outputMode="RGB")
            except Exception:
                converted = None
        img, icc = (converted or img.convert("RGB")), None
    elif mode in ("P", "PA"):
        img = img.convert("RGBA" if (mode == "PA" or "transparency" in img.info) else "RGB")
    elif mode == "RGBa":
        img = img.convert("RGBA")
    elif mode == "La":
        img = img.convert("LA")
    elif mode == "L" and "transparency" in img.info:
        img = img.convert("LA")
    elif mode == "RGB" and "transparency" in img.info:
        img = img.convert("RGBA")
    elif mode not in ("L", "LA", "RGB", "RGBA"):
        img = img.convert("RGB")
    if img.mode in ("RGBA", "LA") and img.getchannel("A").getextrema()[0] == 255:
        img = img.convert("RGB" if img.mode == "RGBA" else "L")   # opaque alpha only costs bytes
    return img, icc, img.mode in ("RGBA", "LA")


def _resize_to(img: Image.Image, max_dim: int) -> Image.Image:
    w, h = img.size
    if not max_dim or max(w, h) <= max_dim:
        return img
    scale = max_dim / max(w, h)
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    return img.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)


def _is_graphic(img: Image.Image) -> bool:
    """Few distinct colours => screenshot / logo / pixel art; lossless usually wins there."""
    probe = img if img.width * img.height <= 4_000_000 else img.resize((img.width // 2, img.height // 2), Image.Resampling.NEAREST)
    return probe.getcolors(maxcolors=4096) is not None


def _guard_target(q: int) -> float:
    # Worst-tile SSIM a normal photo reaches at quality q (calibrated on real photos and screenshots).
    return 0.72 + 0.0024 * q


GUARD_FLOOR = 0.70


def _flatten_for_compare(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGBA", img.size, (128, 128, 128, 255))
        bg.alpha_composite(img.convert("RGBA"))
        return bg.convert("RGB")
    return img.convert("RGB")


def _ssim(a, b, k: int = 7) -> float:
    import numpy as np
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2

    def box(x):
        c = np.pad(x.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
        return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)

    ma, mb = box(a), box(b)
    va, vb, cov = box(a * a) - ma * ma, box(b * b) - mb * mb, box(a * b) - ma * mb
    return float((((2 * ma * mb + c1) * (2 * cov + c2)) / ((ma * ma + mb * mb + c1) * (va + vb + c2))).mean())


class _Reference:
    """Pre-computed view of the source used to score every encode attempt."""

    TILE = 384

    def __init__(self, img: Image.Image):
        import numpy as np
        self.np = np
        self.size = img.size
        self.has_alpha = img.mode in ("RGBA", "LA")
        flat = _flatten_for_compare(img)
        self.luma = np.asarray(flat.convert("L"), dtype=np.float64)
        self.means = ImageStat.Stat(flat).mean
        self.alpha_mean = ImageStat.Stat(img.getchannel("A")).mean[0] if self.has_alpha else None
        self.tiles = self._detail_tiles()

    def _detail_tiles(self):
        np, t = self.np, self.TILE
        h, w = self.luma.shape
        if h <= t or w <= t:
            return [(0, 0, h, w)]
        spots = []
        for y in np.linspace(0, h - t, 4).astype(int):
            for x in np.linspace(0, w - t, 4).astype(int):
                spots.append((float(self.luma[y:y + t, x:x + t].var()), int(y), int(x)))
        spots.sort(reverse=True)
        return [(y, x, y + t, x + t) for _, y, x in spots[:4]]

    def score(self, decoded: Image.Image) -> tuple:
        """Return (worst detail-tile SSIM, problem or None)."""
        if decoded.size != self.size:
            return 0.0, f"size changed to {decoded.size[0]}x{decoded.size[1]}"
        flat = _flatten_for_compare(decoded)
        luma = self.np.asarray(flat.convert("L"), dtype=self.np.float64)
        drift = max(abs(a - b) for a, b in zip(self.means, ImageStat.Stat(flat).mean))
        if drift > 4.0:
            return 0.0, f"colour shift of {drift:.1f} levels"
        if self.has_alpha:
            if decoded.mode not in ("RGBA", "LA"):
                return 0.0, "transparency lost"
            if abs(self.alpha_mean - ImageStat.Stat(decoded.getchannel("A")).mean[0]) > 2.0:
                return 0.0, "transparency changed"
        worst = min(_ssim(self.luma[y0:y1, x0:x1], luma[y0:y1, x0:x1]) for y0, x0, y1, x1 in self.tiles)
        return worst, None


def _encode(img: Image.Image, fmt: str, opts: dict, meta: dict, log: list) -> bytes:
    """Encode to memory. Retries with slimmer metadata if a container rejects it."""
    meta_sets = []
    if any(meta.values()):
        full = {}
        if meta.get("exif"):
            full["exif"] = meta["exif"]
        if meta.get("icc") and fmt in ("JPEG", "WEBP", "PNG", "AVIF"):
            full["icc_profile"] = meta["icc"]
        if meta.get("xmp") and fmt in ("JPEG", "WEBP", "JXL", "AVIF"):
            full["xmp"] = meta["xmp"]
        meta_sets.append(full)
        if "exif" in full:
            meta_sets.append({k: v for k, v in full.items() if k != "exif"})
    meta_sets.append({})
    last_error = None
    for i, extra in enumerate(meta_sets):
        buf = io.BytesIO()
        try:
            try:
                img.save(buf, format=fmt, **opts, **extra)
            except OSError as e:
                # Pillow sizes the optimize/progressive JPEG buffer at ~1 byte per pixel, which very
                # noisy 4:4:4 images overflow; a baseline encode streams and has no such limit.
                if fmt != "JPEG" or "broken data stream" not in str(e) or not (opts.get("optimize") or opts.get("progressive")):
                    raise
                buf = io.BytesIO()
                img.save(buf, format=fmt, **{**opts, "optimize": False, "progressive": False}, **extra)
            if i > 0:
                log.append(("warn", f"{fmt} rejected part of the metadata ({last_error}); saved without it."))
            return buf.getvalue()
        except (ValueError, OSError, TypeError) as e:
            last_error = e
    raise last_error


def _pick_target(src_format: str, choice: str, has_alpha: bool, log: list, name: str) -> str:
    if choice == "original":
        if src_format in ("JPEG", "MPO"):
            return "JPEG" if not has_alpha else "WEBP"
        if src_format in ("PNG", "WEBP"):
            return src_format
        if src_format == "JXL":
            return "JXL" if HAS_JXL else "WEBP"
        if src_format == "GIF":
            return "PNG"
        return "WEBP" if has_alpha else "JPEG"
    if choice == "jpeg" and has_alpha:
        log.append(("info", f"{name}: has transparency, which JPEG cannot store, so saving as WebP instead."))
        return "WEBP"
    if choice == "jxl" and not HAS_JXL:
        log.append(("warn", "JPEG XL encoder not installed (pip install pillow-jxl-plugin), using WebP."))
        return "WEBP"
    return {"jpeg": "JPEG", "jxl": "JXL"}.get(choice, "WEBP")


EXT_FOR = {"JPEG": ".jpg", "WEBP": ".webp", "JXL": ".jxl", "PNG": ".png", "GIF": ".gif", "AVIF": ".avif"}


def _lossy_opts(fmt: str, q: int, pixels: int, threads: int) -> dict:
    if fmt == "JPEG":
        return {"quality": q, "optimize": True, "progressive": True, "subsampling": 0 if q >= 90 else 2}
    if fmt == "WEBP":
        method = 6 if pixels <= 2_000_000 else 5 if pixels <= 8_000_000 else 4
        return {"quality": q, "method": method, "alpha_quality": 100, "exact": False}
    if fmt == "JXL":
        return {"quality": q, "effort": 7, "lossless": False, "lossless_jpeg": False, "num_threads": threads}
    if fmt == "AVIF":
        return {"quality": q, "speed": 6}
    return {}


def _encode_still(im: Image.Image, job: dict, log: list) -> dict:
    name, choice, q = job["name"], job["fmt"], int(job["quality"])
    src_format = (im.format or "").upper()
    if src_format == "TIFF" and getattr(im, "n_frames", 1) > 1:
        raise _Skip("multipage", "multi-page TIFF; kept as-is so no pages are lost")

    meta = _capture_metadata(im) if job["preserve"] else {"exif": None, "xmp": None, "icc": None}
    icc_source = im.info.get("icc_profile")
    try:
        orientation = im.getexif().get(0x0112, 1)
    except Exception:
        orientation = 1
    img = ImageOps.exif_transpose(im) if orientation not in (1, None) else im
    img.load()
    if orientation not in (1, None):
        meta["exif"] = _exif_reset_orientation(meta["exif"])
        meta["xmp"] = _xmp_reset_orientation(meta["xmp"])

    img, icc_source, has_alpha = _normalize(img, icc_source)
    meta["icc"] = icc_source   # colour data, not private metadata: always kept
    target = _pick_target(src_format, choice, has_alpha, log, name)

    max_dim = int(job.get("max_dim") or 0)
    if target == "WEBP" and max(img.size) > WEBP_MAX_DIM:
        max_dim = min(max_dim or WEBP_MAX_DIM, WEBP_MAX_DIM)
        log.append(("warn", f"{name}: {img.width}x{img.height} exceeds WebP's 16383 px limit, so downscaling to fit."))
    img = _resize_to(img, max_dim)

    if target == "JXL":
        img, leftover = _to_srgb(img, icc_source)
        if leftover:
            log.append(("warn", f"{name}: colour profile could not be converted for JPEG XL."))
        meta["icc"] = None
    if target == "WEBP" and img.mode in ("L", "LA"):
        img = img.convert("RGBA" if has_alpha else "RGB")

    pixels = img.width * img.height
    threads = int(job.get("threads", 1))

    if target == "PNG":
        data = _encode(img, "PNG", {"optimize": True}, meta, log)
        return {"data": data, "fmt": "PNG", "detail": "lossless", "ssim": None}

    candidates = []
    lossless_src = src_format in ("PNG", "GIF", "BMP", "TIFF")
    if target in ("WEBP", "JXL") and lossless_src and _is_graphic(img):
        opts = {"lossless": True, "quality": 80, "method": 4} if target == "WEBP" else \
               {"lossless": True, "effort": 7, "num_threads": threads}
        candidates.append({"data": _encode(img, target, opts, meta, log), "fmt": target, "detail": "lossless", "ssim": None})

    reference = None
    if job.get("guard"):
        try:
            reference = _Reference(img)
        except ImportError:
            log.append(("warn", "NumPy missing, so the quality guard is disabled (pip install numpy)."))

    best, quality, problem = None, q, None
    for attempt in range(3):
        data = _encode(img, target, _lossy_opts(target, quality, pixels, threads), meta, log)
        result = {"data": data, "fmt": target, "detail": f"q{quality}", "ssim": None}
        if reference is None:
            best = result
            break
        with Image.open(io.BytesIO(data)) as dec:
            dec.load()
            score, problem = reference.score(dec)
        result["ssim"] = score
        if not problem and (best is None or score > (best["ssim"] or 0)):
            best = result
        if not problem and (score >= _guard_target(q) or quality >= 97):
            best = result
            break
        # A colour shift usually comes from chroma subsampling on saturated colours; JPEG keeps full chroma from q90.
        new_quality = min(97, max(quality + 8, 90) if problem else quality + 8)
        if new_quality == quality:
            break
        what = problem if problem else f"detail check scored {score:.3f}"
        log.append(("info", f"{name}: {what} at q{quality}, so retrying at q{new_quality}."))
        quality = new_quality

    if best is None or (best["ssim"] is not None and best["ssim"] < GUARD_FLOOR):
        # Lossy can't reach the quality bar (WebP is always 4:2:0); exact encoding still can.
        if target in ("WEBP", "JXL") and not (candidates and candidates[0]["detail"] == "lossless"):
            opts = {"lossless": True, "quality": 80, "method": 4} if target == "WEBP" else                    {"lossless": True, "effort": 7, "num_threads": threads}
            log.append(("info", f"{name}: lossy {target} failed the quality check, so trying lossless."))
            candidates.insert(0, {"data": _encode(img, target, opts, meta, log), "fmt": target,
                                  "detail": "lossless", "ssim": None})
        if candidates:
            return candidates[0]
        why = problem or f"SSIM {best['ssim']:.3f}"
        raise _Skip("quality", f"could not compress without visible loss ({why}); original kept")
    candidates.append(best)

    # Prefer the exact (lossless) encode unless lossy is clearly smaller.
    if len(candidates) > 1 and len(candidates[0]["data"]) <= len(candidates[1]["data"]) * 1.15:
        return candidates[0]
    return candidates[-1]


def _encode_animation(im: Image.Image, job: dict, log: list) -> dict:
    choice, q = job["fmt"], int(job["quality"])
    src_format = (im.format or "").upper()
    n = im.n_frames
    if im.width * im.height * n > 600_000_000:
        raise _Skip("too_large", f"animation too large to re-encode safely ({n} frames); kept as-is")
    target = "GIF" if (choice == "original" and src_format == "GIF") else "WEBP"
    frames, durations = [], []
    max_dim = int(job.get("max_dim") or 0)
    for frame in ImageSequence.Iterator(im):
        durations.append(int(frame.info.get("duration", im.info.get("duration", 100)) or 100))
        f = frame.copy() if target == "GIF" else frame.convert("RGBA")
        frames.append(_resize_to(f, max_dim))
    loop = im.info.get("loop", 0)
    if target == "GIF":
        opts = {"save_all": True, "append_images": frames[1:], "optimize": True, "duration": durations,
                "loop": loop}
        detail = "optimised"
    else:
        opts = {"save_all": True, "append_images": frames[1:], "duration": durations, "loop": loop,
                "quality": q, "method": 4, "allow_mixed": True}
        detail = f"q{q} · {n} frames"
    meta = _capture_metadata(im) if job["preserve"] and target == "WEBP" else {"exif": None, "xmp": None, "icc": None}
    data = _encode(frames[0], target, opts, meta, log)
    with Image.open(io.BytesIO(data)) as dec:
        if getattr(dec, "n_frames", 1) != n:
            raise RuntimeError(f"animation check failed: {getattr(dec, 'n_frames', 1)} of {n} frames written")
    return {"data": data, "fmt": target, "detail": detail, "ssim": None}


def _copy_original(job: dict) -> Optional[str]:
    dest = os.path.join(job["dest_dir"], job["name"])
    if os.path.exists(dest) and os.path.getsize(dest) == job["size"]:
        return dest
    os.makedirs(job["dest_dir"], exist_ok=True)
    tmp = os.path.join(job["dest_dir"], f"{TEMP_PREFIX}{uuid.uuid4().hex}.tmp")
    shutil.copy2(job["path"], tmp)
    os.replace(tmp, dest)
    return dest


def _skip_result(res: dict, job: dict, reason: str, message: str) -> dict:
    res.update(status="skipped", reason=reason, message=message)
    if job.get("copy_on_skip") and reason in ("not_smaller", "multipage", "too_large", "small", "quality"):
        try:
            _copy_original(job)
            res["message"] += ", original copied to output"
        except OSError as e:
            res["log"].append(("warn", f"{job['name']}: could not copy original ({e})"))
    return res


def compress_image(job: dict) -> dict:
    """Compress one image. Runs in a worker process, or a thread in watch mode."""
    res = _new_result(job)
    log = res["log"]
    if _worker_cancelled():
        res.update(status="cancelled", reason="cancelled", message="cancelled")
        return res
    _worker_wait_running()
    if _worker_cancelled():
        res.update(status="cancelled", reason="cancelled", message="cancelled")
        return res

    name, size, t0 = job["name"], job["size"], time.perf_counter()
    tmp = None
    try:
        with Image.open(job["path"]) as im:
            animated = getattr(im, "is_animated", False) and getattr(im, "n_frames", 1) > 1 \
                and (im.format or "").upper() in ("GIF", "WEBP", "PNG")
            out = _encode_animation(im, job, log) if animated else _encode_still(im, job, log)

        data = out["data"]
        if len(data) >= size:
            return _skip_result(res, job, "not_smaller",
                                f"already optimal ({fmt_bytes(len(data))} would not beat {fmt_bytes(size)})")

        final = os.path.join(job["dest_dir"], job["stem"] + EXT_FOR[out["fmt"]])
        if os.path.exists(final) and not job.get("overwrite"):
            return _skip_result(res, job, "exists", f"output already exists: {os.path.basename(final)}")
        os.makedirs(job["dest_dir"], exist_ok=True)
        tmp = os.path.join(job["dest_dir"], f"{TEMP_PREFIX}{uuid.uuid4().hex}.tmp{EXT_FOR[out['fmt']]}")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, final)
        tmp = None
        if job["preserve"]:
            _copy_times(job["path"], final)

        saved = 1 - len(data) / size
        ssim = f" · SSIM {out['ssim']:.3f}" if out.get("ssim") else ""
        res.update(status="success", out_path=final, out_size=len(data),
                   message=f"{fmt_bytes(size)} → {fmt_bytes(len(data))}")
        log.append(("done", f"{name} → {os.path.basename(final)} · {fmt_bytes(size)} → {fmt_bytes(len(data))} "
                            f"(−{saved:.0%}) · {out['fmt']} {out['detail']}{ssim} · {time.perf_counter() - t0:.1f}s"))
        return res
    except _Skip as s:
        return _skip_result(res, job, s.reason, s.message)
    except UnidentifiedImageError:
        hint = " (HEIC support needs: pip install pillow-heif)" if job["path"].lower().endswith((".heic", ".heif")) and not HAS_HEIF else ""
        res.update(message=f"unsupported or corrupt image{hint}")
    except Image.DecompressionBombError as e:
        res.update(message=f"image too large to decode safely: {e}")
    except (OSError, SyntaxError) as e:
        res.update(message=f"corrupt or truncated image: {e}")
    except MemoryError:
        res.update(message="out of memory while encoding")
    except Exception as e:
        res.update(message=f"{type(e).__name__}: {e}")
    finally:
        _remove_quietly(tmp)
    log.append(("error", f"{name}: {res['message']}"))
    return res


# ---------------------------------------------------------------------------
# Videos
# ---------------------------------------------------------------------------

@dataclass
class Probe:
    ok: bool = False
    duration: float = 0.0
    video_index: int = -1
    width: int = 0
    height: int = 0
    fps: Optional[Fraction] = None
    vcodec: str = ""
    pix_fmt: str = ""
    rotation: int = 0
    interlaced: bool = False
    decodable: bool = False
    audio: list = field(default_factory=list)
    subtitles: list = field(default_factory=list)
    attachments: list = field(default_factory=list)


def _is_attached_pic(stream) -> bool:
    try:
        import av
        return bool(stream.disposition & av.stream.Disposition.attached_pic)
    except Exception:
        return False


def probe_media(path: str, decode_first: bool = True) -> Probe:
    import av
    p = Probe()
    with av.open(path) as c:
        if c.duration:
            p.duration = c.duration / 1_000_000
        video = None
        for st in c.streams:
            cc = getattr(st, "codec_context", None)
            if st.type == "video":
                if video is None and not _is_attached_pic(st):
                    video = st
            elif st.type == "audio":
                channels = getattr(cc, "channels", 0) or getattr(getattr(cc, "layout", None), "nb_channels", 0) or 2
                p.audio.append({"index": st.index, "codec": cc.name if cc else "", "channels": channels,
                                "bit_rate": st.bit_rate or (cc.bit_rate if cc else 0) or 0})
            elif st.type == "subtitle":
                p.subtitles.append({"index": st.index, "codec": cc.name if cc else ""})
            elif st.type == "attachment":
                p.attachments.append(st.index)
        if video is not None:
            cc = video.codec_context
            p.video_index = video.index
            p.width, p.height = cc.width, cc.height
            p.vcodec, p.pix_fmt = cc.name, cc.pix_fmt or ""
            rate = video.average_rate or video.guessed_rate or video.base_rate
            p.fps = Fraction(rate) if rate else None
            if not p.duration and video.duration and video.time_base:
                p.duration = float(video.duration * video.time_base)
            if decode_first:
                try:
                    cc.thread_type = "AUTO"
                    for frame in c.decode(video):
                        p.rotation = int(getattr(frame, "rotation", 0) or 0) % 360
                        p.interlaced = bool(getattr(frame, "interlaced_frame", False))
                        p.decodable = True
                        break
                except Exception:
                    p.decodable = False
        p.ok = True
    return p


def _verify_video(path: str, src: Probe, expect_audio: int) -> Optional[str]:
    """Return a problem description, or None if the output is complete and decodable."""
    import av
    try:
        out = probe_media(path)
    except Exception as e:
        return f"output unreadable ({e})"
    if out.video_index < 0 or not out.decodable:
        return "output has no decodable video"
    if src.duration > 1 and out.duration > 0:
        tolerance = max(1.0, src.duration * 0.02)
        if abs(out.duration - src.duration) > tolerance:
            return f"duration mismatch ({out.duration:.1f}s vs source {src.duration:.1f}s)"
    if len(out.audio) < expect_audio:
        return f"{expect_audio - len(out.audio)} audio track(s) missing"
    if out.duration > 3:
        try:
            with av.open(path) as c:
                c.seek(int((out.duration - 2) * 1_000_000))
                if next(c.decode(c.streams.video[0]), None) is None:
                    return "end of file not decodable"
        except Exception as e:
            return f"end of file not decodable ({e})"
    return None


TEXT_SUBS = {"subrip", "ass", "ssa", "webvtt", "text"}
IMAGE_SUBS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}
COPY_AUDIO = {"aac", "opus", "vorbis", "mp3", "ac3", "eac3"}
MODE_LABEL = {"cuda": "GPU zero-copy", "nvdec": "GPU decode", "hw": "HW decode", "sw": "CPU decode"}


def _scale_plan(w: int, h: int, max_height: int) -> tuple:
    """Return (needs_scale, out_w, out_h, ffmpeg_w_expr, ffmpeg_h_expr).

    The expressions are orientation-agnostic so portrait and rotated phone
    footage is bounded by the same box as landscape footage.
    """
    long_edge = round(max_height * 16 / 9) if max_height else 0
    f = f"min(1\\,min({long_edge}/max(iw\\,ih)\\,{max_height}/min(iw\\,ih)))"
    if max_height and not (w and h):   # stream info unavailable: let FFmpeg evaluate the bound
        return True, 0, 0, f"trunc(iw*{f}/2)*2", f"trunc(ih*{f}/2)*2"
    factor = 1.0
    if max_height and w and h:
        factor = min(1.0, long_edge / max(w, h), max_height / min(w, h))
    out_w, out_h = int(w * factor / 2) * 2, int(h * factor / 2) * 2
    odd = bool(w % 2 or h % 2)
    if factor >= 1.0 and not odd:
        return False, w, h, "", ""
    if factor >= 1.0:
        return True, out_w, out_h, "trunc(iw/2)*2", "trunc(ih/2)*2"
    return True, out_w, out_h, f"trunc(iw*{f}/2)*2", f"trunc(ih*{f}/2)*2"


def _fps_plan(fps: Optional[Fraction], cap: bool) -> Optional[Fraction]:
    """Cap at ~30 fps by dropping whole frames (60→30, 50→25, 120→30), never uneven ratios."""
    if not cap or not fps or float(fps) <= 32:
        return None
    divisor = max(2, round(float(fps) / 30))
    return (fps / divisor).limit_denominator(1001)


def _decode_modes(hw: Hardware, enc: Encoder, p: Probe, needs_cpu_filters: bool) -> list:
    if enc.vendor == "nvidia":
        modes = []
        if (hw.cuda and p.ok and p.rotation == 0 and p.vcodec in NVDEC_CODECS and p.pix_fmt in NVDEC_PIXFMTS
                and (not p.interlaced or hw.cuda_deint) and not needs_cpu_filters):
            modes.append("cuda")
        return modes + ["nvdec", "sw"]
    if enc.vendor in ("amd", "intel", "apple"):
        return ["hw", "sw"]
    return ["sw"]


def _audio_args(p: Probe, hw: Hardware, force_aac: bool) -> list:
    if not p.ok:
        return ["-c:a", "aac", "-b:a", "160k"]
    args = []
    for i, a in enumerate(p.audio):
        ch, br = max(1, a["channels"]), a["bit_rate"]
        if a["codec"] in COPY_AUDIO and (br == 0 or br <= 64_000 * ch + 32_000):
            args += [f"-c:a:{i}", "copy"]
        elif ch <= 2 and hw.opus and not force_aac:
            args += [f"-c:a:{i}", "libopus", f"-b:a:{i}", "64k" if ch == 1 else "96k"]
        else:
            args += [f"-c:a:{i}", "aac", f"-b:a:{i}", f"{min(64 * ch, 512)}k"]
    return args


def _video_cq(q: int, out_h: int) -> int:
    short = out_h
    if short and short <= 720:
        return max(10, q - 2)      # small frames show artefacts sooner
    if short >= 2000:
        return q + 2               # 4K hides more
    return q


def _run_ffmpeg(cmd: list, ctl: Control, duration: float, on_progress: Optional[Callable]) -> tuple:
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            creationflags=NO_WINDOW | BELOW_NORMAL)
    ctl.register(proc)
    tail = deque(maxlen=60)
    drain = threading.Thread(target=lambda: tail.extend(line.rstrip() for line in proc.stderr), daemon=True)
    drain.start()
    stats = {}
    try:
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            stats[key] = value
            if key == "progress":
                if on_progress:
                    try:
                        t = int(stats.get("out_time_us", "0")) / 1_000_000
                    except ValueError:
                        t = 0.0
                    pct = min(0.999, t / duration) if duration > 0 else 0.0
                    on_progress({"pct": pct, "fps": stats.get("fps", ""), "speed": stats.get("speed", "").strip()})
            if ctl.cancelled:
                proc.kill()
                break
        rc = proc.wait()
    finally:
        ctl.unregister(proc)
        drain.join(2)
    return rc, list(tail)


def _ffmpeg_error(tail: list) -> str:
    lines = [l for l in tail if l.strip()]
    return lines[-1][:240] if lines else "FFmpeg exited with an error"


def compress_video(job: dict, hw: Hardware, ctl: Control, report: Optional[Callable] = None) -> dict:
    """Transcode one video. Runs in a thread; the heavy lifting happens in FFmpeg."""
    res = _new_result(job)
    log = res["log"]
    name, size, src = job["name"], job["size"], job["path"]
    ctl.wait_running()
    if ctl.cancelled:
        res.update(status="cancelled", reason="cancelled", message="cancelled")
        return res
    if not hw.ffmpeg:
        res.update(message="FFmpeg not available")
        log.append(("error", f"{name}: FFmpeg not available"))
        return res

    t0 = time.monotonic()
    try:
        probe = probe_media(src)
    except Exception as e:
        probe = Probe()
        log.append(("warn", f"{name}: could not inspect streams ({e}); using generic mapping."))
    if probe.ok and probe.video_index < 0:
        return _skip_result(res, job, "no_video", "no video stream (audio-only file)")

    chain = hw.chain(job["codec"])[:3]
    if not chain:
        res.update(message="no working video encoder found")
        log.append(("error", f"{name}: no working video encoder found"))
        return res

    w, h = (probe.height, probe.width) if probe.rotation in (90, 270) else (probe.width, probe.height)
    needs_scale, out_w, out_h, wexpr, hexpr = _scale_plan(w, h, int(job["max_height"]))
    fps = _fps_plan(probe.fps, job["fps_cap"])
    cq = _video_cq(int(job["quality"]), min(out_w, out_h) if out_w and out_h else 1080)
    final = os.path.join(job["dest_dir"], job["stem"] + ".mkv")
    if os.path.exists(final) and not job["overwrite"]:
        return _skip_result(res, job, "exists", f"output already exists: {os.path.basename(final)}")
    os.makedirs(job["dest_dir"], exist_ok=True)
    tmp = os.path.join(job["dest_dir"], f"{TEMP_PREFIX}{uuid.uuid4().hex}.tmp.mkv")

    subs = [s for s in probe.subtitles if s["codec"] in TEXT_SUBS | IMAGE_SUBS | {"mov_text"}]
    expect_audio = len(probe.audio) if probe.ok else 0
    if probe.ok:
        dims = f"{w}x{h}" + (f" → {out_w}x{out_h}" if needs_scale else "")
        rate = f"{float(probe.fps):.2f} fps" if probe.fps else "VFR"
        rate += f" → {float(fps):.2f}" if fps else ""
        extras = []
        if probe.rotation:
            extras.append(f"rotated {probe.rotation}°")
        if probe.interlaced:
            extras.append("interlaced → deinterlace")
        extra = f" · {', '.join(extras)}" if extras else ""
        log.append(("start", f"{name} · {fmt_bytes(size)} · {probe.vcodec} {dims} · {rate} · "
                             f"{fmt_duration(probe.duration)} · {len(probe.audio)} audio, {len(subs)} subs{extra}"))

    include_subs, force_aac = True, False
    used, last_error = None, ""

    def progress(enc, mode):
        def cb(d):
            if report:
                report({**d, "encoder": enc.label, "mode": MODE_LABEL[mode]})
        return cb

    def build(enc, mode):
        cmd = [hw.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
               "-progress", "pipe:1", "-nostats", "-stats_period", "0.5"]
        if mode == "cuda":
            cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-extra_hw_frames", "8"]
        elif mode == "nvdec":
            cmd += ["-hwaccel", "cuda"]
        elif mode == "hw":
            cmd += ["-hwaccel", "auto"]
        if os.path.splitext(src)[1].lower() in (".avi", ".flv", ".wmv", ".mpg", ".mpeg"):
            cmd += ["-fflags", "+genpts"]
        cmd += ["-i", src]
        active_subs = subs if include_subs else []
        if probe.ok:
            cmd += ["-map", f"0:{probe.video_index}"]
            for a in probe.audio:
                cmd += ["-map", f"0:{a['index']}"]
            for s in active_subs:
                cmd += ["-map", f"0:{s['index']}"]
            if active_subs:
                for t in probe.attachments:
                    cmd += ["-map", f"0:{t}"]
        else:
            cmd += ["-map", "0:V:0", "-map", "0:a?"]

        chain_f = []
        if fps:
            chain_f.append(f"fps={fps.numerator}/{fps.denominator}")
        if mode == "cuda":
            if probe.interlaced:
                chain_f.append("bwdif_cuda=deint=interlaced")
            opts = [f"w={wexpr}", f"h={hexpr}", "interp_algo=lanczos"] if needs_scale else []
            chain_f.append("scale_cuda=" + ":".join(opts + [f"format={enc.pix_fmt}"]))
        else:
            if probe.interlaced:
                chain_f.append("bwdif=mode=send_frame:deint=interlaced")
            if needs_scale:
                chain_f.append(f"scale=w={wexpr}:h={hexpr}:flags=lanczos")
            chain_f.append(f"format={enc.pix_fmt}")
        cmd += ["-vf", ",".join(chain_f)]
        cmd += ["-c:v", enc.name, *_encoder_tiers(enc.name, enc.family, cq)[enc.tier]]
        cmd += _audio_args(probe, hw, force_aac)
        for i, s in enumerate(active_subs):
            cmd += [f"-c:s:{i}", "srt" if s["codec"] == "mov_text" else "copy"]
        if active_subs and probe.attachments:
            cmd += ["-c:t", "copy"]
        if job["preserve"]:
            cmd += ["-map_metadata", "0", "-map_chapters", "0"]
        else:
            cmd += ["-map_metadata", "-1", "-map_chapters", "-1"]
        cmd += ["-max_muxing_queue_size", "4096", tmp]
        return cmd

    try:
        for enc in chain:
            for mode in _decode_modes(hw, enc, probe, needs_cpu_filters=False):
                for _ in range(3):   # stream-level fallbacks: drop exotic subtitles, Opus → AAC
                    ctl.wait_running()
                    if ctl.cancelled:
                        raise CancelledError
                    rc, tail = _run_ffmpeg(build(enc, mode), ctl, probe.duration, progress(enc, mode))
                    if ctl.cancelled:
                        raise CancelledError
                    if rc == 0:
                        problem = _verify_video(tmp, probe, expect_audio)
                        if problem is None:
                            used = (enc, mode)
                        else:
                            last_error = problem
                            log.append(("warn", f"{name}: {enc.label} ({MODE_LABEL[mode]}) output rejected: {problem}"))
                        break
                    text = "\n".join(tail)
                    last_error = _ffmpeg_error(tail)
                    if include_subs and subs and "ubtitle" in text:
                        include_subs = False
                        log.append(("warn", f"{name}: subtitle track could not be carried over, so retrying without subtitles."))
                        continue
                    if not force_aac and "opus" in text.lower():
                        force_aac = True
                        continue
                    log.append(("warn", f"{name}: {enc.label} ({MODE_LABEL[mode]}) failed: {last_error}"))
                    break
                if used:
                    break
                _remove_quietly(tmp)
            if used:
                break

        if not used:
            res.update(message=f"all encoders failed: {last_error}")
            log.append(("error", f"{name}: {res['message']}"))
            return res

        new_size = os.path.getsize(tmp)
        elapsed = time.monotonic() - t0
        speed = f" · {probe.duration / elapsed:.1f}x realtime" if probe.duration and elapsed else ""
        if new_size >= size:
            return _skip_result(res, job, "not_smaller",
                                f"already optimal ({fmt_bytes(new_size)} would not beat {fmt_bytes(size)})")
        os.replace(tmp, final)
        if job["preserve"]:
            _copy_times(src, final)
        enc, mode = used
        res.update(status="success", out_path=final, out_size=new_size,
                   message=f"{fmt_bytes(size)} → {fmt_bytes(new_size)}")
        log.append(("done", f"{name} → {os.path.basename(final)} · {fmt_bytes(size)} → {fmt_bytes(new_size)} "
                            f"(−{1 - new_size / size:.0%}) · {enc.label} · {MODE_LABEL[mode]} · CQ {_family_quality(enc.family, cq)}"
                            f" · {fmt_duration(elapsed)}{speed}"))
        res["encoder"] = enc.label
        return res
    except CancelledError:
        res.update(status="cancelled", reason="cancelled", message="cancelled")
        return res
    except Exception as e:
        res.update(message=f"{type(e).__name__}: {e}")
        log.append(("error", f"{name}: {res['message']}"))
        return res
    finally:
        _remove_quietly(tmp)


# ---------------------------------------------------------------------------
# Batch session
# ---------------------------------------------------------------------------

def make_job(path: str, name: str, size: int, kind: str, fp: str, dest_dir: str, stem: str,
             s: Settings, threads: int = 1) -> dict:
    job = {"path": path, "name": name, "size": size, "kind": kind, "fp": fp, "dest_dir": dest_dir,
           "stem": stem, "preserve": s.preserve_metadata, "overwrite": s.overwrite,
           "copy_on_skip": s.copy_on_skip}
    if kind == "image":
        job.update(fmt=s.img_format, quality=s.img_quality, max_dim=s.img_max_dim,
                   guard=s.quality_guard, threads=threads)
    else:
        job.update(codec=s.vid_codec, quality=s.vid_quality, max_height=s.vid_max_height,
                   fps_cap=s.vid_fps_cap)
    return job


def move_original(path: str, folder: str, log: Optional[LogFn] = None) -> bool:
    """Move a source file into <its folder>/<folder>, never overwriting anything."""
    if not os.path.exists(path):
        return False
    dest_dir = os.path.join(os.path.dirname(path), folder)
    os.makedirs(dest_dir, exist_ok=True)
    base, ext = os.path.splitext(os.path.basename(path))
    dest, n = os.path.join(dest_dir, base + ext), 1
    while os.path.exists(dest):
        dest = os.path.join(dest_dir, f"{base} ({n}){ext}")
        n += 1
    for _ in range(8):
        try:
            shutil.move(path, dest)
            return True
        except OSError:
            time.sleep(0.25)
    if log:
        log("warn", f"Could not move {os.path.basename(path)} to {folder} (file in use?)")
    return False


# Skip reasons that will not change on a rerun with identical settings.
STABLE_SKIPS = frozenset({"not_smaller", "multipage", "too_large", "no_video", "quality"})


def process_single(path: str, in_root: str, s: Settings, hw: Hardware, history: History,
                   ctl: Control, log: LogFn) -> dict:
    """Compress one file outside a batch (watch-folder mode) with full history bookkeeping."""
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    kind = "image" if ext in IMAGE_EXTS else "video"
    size = os.path.getsize(path)
    fp = file_fingerprint(path)
    key = s.image_key() if kind == "image" else s.video_key(hw)
    if not s.overwrite:
        prior = history.processed_output(fp)
        if prior and os.path.exists(prior):
            log("skip", f"{name}: already compressed in an earlier run")
            return {"status": "skipped", "reason": "history"}
        if history.has_verdict(fp, key):
            log("skip", f"{name}: already optimal at these settings")
            return {"status": "skipped", "reason": "optimal"}
    rel = os.path.relpath(os.path.dirname(path), in_root)
    stem = os.path.splitext(name)[0] + (f"_{fp[:8]}" if s.append_hash else "")
    job = make_job(path, name, size, kind, fp, os.path.join(os.path.abspath(s.output_dir), "" if rel == "." else rel),
                   stem, s, threads=-1)
    if size < (MIN_IMAGE_BYTES if kind == "image" else MIN_VIDEO_BYTES):
        res = _skip_result(_new_result(job), job, "small", "file too small to bother")
    elif kind == "image":
        res = compress_image(job)
    else:
        res = compress_video(job, hw, ctl)
    for level, msg in res["log"]:
        log(level, msg)
    status, reason = res["status"], res.get("reason", "")
    if status == "skipped" and reason != "cancelled":
        log("skip", f"{name}: {res['message']}")
    if status == "success":
        history.mark_processed(fp, res["out_path"])
    elif status == "skipped" and reason in STABLE_SKIPS:
        history.mark_verdict(fp, key)
    if status in ("success", "skipped") and reason not in ("cancelled", "exists") and s.move_processed:
        move_original(path, "PROCESSED", log)
    elif status == "error" and s.move_failed:
        move_original(path, "FAILED", log)
    return res


class Session:
    """One batch run: scan → fingerprint → plan → compress. GUI-agnostic.

    The UI polls snapshot() for progress and receives log lines via `log`.
    """

    def __init__(self, settings: Settings, hw: Hardware, history_path: str, log: LogFn):
        self.s = settings
        self.hw = hw
        self.history_path = history_path
        self.log = log
        self.ctl = Control()
        self._lock = threading.Lock()
        self.phase = "starting"
        self.phase_detail = ""
        self.total_files = self.total_bytes = 0
        self.done_files = self.done_bytes = 0
        self.compressed = self.skipped = self.failed = 0
        self.bytes_in = self.bytes_out = 0
        self.history_skips = 0
        self.active_videos = {}
        self._img_order: list = []
        self._img_cursor = 0
        self.compress_started = None
        self.started = time.monotonic()
        self.finished = False
        self.outcome = ""

    # -- control -------------------------------------------------------------
    def pause(self):
        self.ctl.pause()

    def resume(self):
        self.ctl.resume()

    def cancel(self):
        self.ctl.cancel()

    @property
    def paused(self) -> bool:
        return self.ctl.paused

    # -- progress ------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            videos = [dict(v) for v in self.active_videos.values()]
            partial = sum(v["size"] * v.get("pct", 0) for v in videos)
            done_bytes = self.done_bytes
        frac = (done_bytes + partial) / self.total_bytes if self.total_bytes else 0.0
        eta = None
        if self.compress_started and frac > 0.01 and not self.paused:
            elapsed = time.monotonic() - self.compress_started
            eta = elapsed * (1 - frac) / frac
        images = []
        order = self._img_order
        for fut, job in order[self._img_cursor:self._img_cursor + 64]:
            if fut.running():
                images.append({"name": job["name"], "kind": "image", "size": job["size"]})
                if len(images) >= 12:
                    break
        return {
            "phase": self.phase, "detail": self.phase_detail, "fraction": min(1.0, frac), "eta": eta,
            "total": self.total_files, "done": self.done_files, "compressed": self.compressed,
            "skipped": self.skipped, "failed": self.failed, "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out, "elapsed": time.monotonic() - self.started,
            "active": videos + images, "paused": self.paused, "finished": self.finished,
        }

    # -- run -----------------------------------------------------------------
    def run(self) -> str:
        self.history = History(self.history_path)
        try:
            self._run()
        except Exception as e:
            self.outcome = "error"
            self.log("error", f"Session stopped by an unexpected error: {type(e).__name__}: {e}")
            for line in traceback.format_exc().splitlines()[-6:]:
                self.log("muted", line)
        finally:
            self.history.close()
            self.finished = True
            self.phase = "done"
        if not self.outcome:
            self.outcome = "cancelled" if self.ctl.cancelled else "completed"
        return self.outcome

    def _run(self):
        s = self.s
        in_root, out_root = os.path.abspath(s.input_dir), os.path.abspath(s.output_dir)
        self._sweep_temp(out_root)

        self.phase = "scanning"
        files = self._scan(in_root, out_root)
        if self.ctl.cancelled:
            return
        n_img = sum(1 for f in files if f["kind"] == "image")
        self.log("info", f"Found {n_img} images and {len(files) - n_img} videos "
                         f"({fmt_bytes(sum(f['size'] for f in files))}).")
        if not files:
            self.outcome = "empty"
            return

        self.phase = "indexing"
        self._fingerprint(files)
        if self.ctl.cancelled:
            return

        self.phase = "planning"
        images, videos = self._plan(files, in_root, out_root)
        if self.ctl.cancelled:
            return

        self.phase = "compressing"
        self._execute(images, videos)

    def _sweep_temp(self, out_root: str):
        removed = 0
        for root, _dirs, names in os.walk(out_root):
            for n in names:
                if TEMP_RE.match(n):
                    _remove_quietly(os.path.join(root, n))
                    removed += 1
        if removed:
            self.log("muted", f"Removed {removed} leftover temp file(s) from an interrupted run.")

    def _scan(self, in_root: str, out_root: str) -> list:
        s = self.s
        out_norm = os.path.normcase(out_root)
        found, stack = [], [in_root]
        while stack:
            if self.ctl.cancelled:
                return []
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError as e:
                self.log("warn", f"Cannot read folder {d}: {e.strerror}")
                continue
            with it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            norm = os.path.normcase(entry.path)
                            if (s.scan_subfolders and entry.name not in SKIP_DIRS
                                    and norm != out_norm and not norm.startswith(out_norm + os.sep)):
                                stack.append(entry.path)
                            continue
                        if not entry.is_file() or entry.name.startswith(TEMP_PREFIX):
                            continue
                        ext = os.path.splitext(entry.name)[1].lower()
                        kind = "image" if (ext in IMAGE_EXTS and s.images) else \
                               "video" if (ext in VIDEO_EXTS and s.videos) else None
                        if not kind:
                            continue
                        st = entry.stat()
                        rel = os.path.relpath(d, in_root)
                        found.append({"path": entry.path, "name": entry.name, "rel": "" if rel == "." else rel,
                                      "kind": kind, "size": st.st_size, "mtime_ns": st.st_mtime_ns})
                    except OSError:
                        continue
            self.phase_detail = f"{len(found)} files"
        found.sort(key=lambda f: f["path"].lower())
        return found

    def _fingerprint(self, files: list):
        cache = self.history.load_fingerprint_cache()
        todo = []
        for f in files:
            hit = cache.get(os.path.normcase(f["path"]))
            if hit and hit[0] == f["size"] and hit[1] == f["mtime_ns"]:
                f["fp"] = hit[2]
            else:
                todo.append(f)
        if not todo:
            return
        self.log("muted", f"Fingerprinting {len(todo)} new or changed file(s)…")
        rows = []
        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as pool:
            futures = {pool.submit(file_fingerprint, f["path"]): f for f in todo}
            for i, fut in enumerate(as_completed(futures), 1):
                f = futures[fut]
                try:
                    f["fp"] = fut.result()
                    rows.append((os.path.normcase(f["path"]), f["size"], f["mtime_ns"], f["fp"]))
                except OSError as e:
                    f["fp"] = None
                    f["error"] = e.strerror or str(e)
                if i % 20 == 0:
                    self.phase_detail = f"{i}/{len(todo)}"
                if self.ctl.cancelled:
                    pool.shutdown(cancel_futures=True)
                    break
        self.history.store_fingerprints(rows)

    def _plan(self, files: list, in_root: str, out_root: str) -> tuple:
        s = self.s
        keys = {"image": s.image_key(), "video": s.video_key(self.hw)}
        seen, jobs = {}, []
        counts = {"history": 0, "optimal": 0, "duplicate": 0, "small": 0}
        for f in files:
            if f.get("fp") is None:
                self._finish(f, {"status": "error", "reason": "unreadable",
                                 "message": f"cannot read file: {f.get('error', 'unknown error')}",
                                 "log": [("error", f"{f['name']}: cannot read file ({f.get('error', '')})")],
                                 "in_size": f["size"], "out_size": 0}, quiet=False)
                continue
            dest_dir = os.path.join(out_root, f["rel"])
            stem = os.path.splitext(f["name"])[0]
            if s.append_hash:
                stem = f"{stem}_{f['fp'][:8]}"
            job = make_job(f["path"], f["name"], f["size"], f["kind"], f["fp"], dest_dir, stem, s)
            minimum = MIN_IMAGE_BYTES if f["kind"] == "image" else MIN_VIDEO_BYTES
            reason = None
            if f["size"] < minimum:
                reason, msg = "small", f"below {fmt_bytes(minimum)}"
            elif not s.overwrite:
                prior = self.history.processed_output(f["fp"])
                if prior and os.path.exists(prior):
                    reason, msg = "history", "already compressed in an earlier run"
                elif self.history.has_verdict(f["fp"], keys[f["kind"]]):
                    reason, msg = "optimal", "already optimal at these settings"
            if reason is None and f["fp"] in seen:
                reason, msg = "duplicate", f"identical to {seen[f['fp']]}"
            if reason:
                counts[reason if reason in counts else "history"] += 1
                self._finish(job, _skip_result(_new_result(job), job, reason, msg), quiet=True)
                continue
            seen[f["fp"]] = f["name"]
            jobs.append(job)

        # Two sources that would land on the same output name (photo.jpg + photo.png) get the
        # source extension appended instead of silently overwriting each other.
        groups = {}
        for job in jobs:
            groups.setdefault((os.path.normcase(job["dest_dir"]), job["stem"].lower(), job["kind"]), []).append(job)
        for group in groups.values():
            if len(group) > 1:
                for job in group:
                    job["stem"] += "_" + os.path.splitext(job["name"])[1].lstrip(".").lower()

        skipped = {k: v for k, v in counts.items() if v}
        if skipped:
            labels = {"history": "already compressed", "optimal": "already optimal", "duplicate": "duplicates",
                      "small": "too small"}
            self.log("skip", "Skipped " + ", ".join(f"{v} {labels[k]}" for k, v in skipped.items()) + ".")
        self.history_skips = sum(counts.values())
        return [j for j in jobs if j["kind"] == "image"], [j for j in jobs if j["kind"] == "video"]

    def _finish(self, job: dict, res: dict, quiet: bool = False):
        s = self.s
        if not quiet:
            for level, msg in res.get("log", []):
                self.log(level, msg)
        status, reason = res["status"], res.get("reason", "")
        if status == "success":
            self.history.mark_processed(job["fp"], res["out_path"])
        elif status == "skipped" and reason in STABLE_SKIPS:
            self.history.mark_verdict(job["fp"], s.image_key() if job["kind"] == "image" else s.video_key(self.hw))
        if status != "cancelled" and reason not in ("exists",):
            if status in ("success", "skipped") and s.move_processed:
                move_original(job["path"], "PROCESSED", self.log)
            elif status == "error" and s.move_failed:
                move_original(job["path"], "FAILED", self.log)
        if status == "skipped" and not quiet and reason not in ("cancelled",):
            self.log("skip", f"{job['name']}: {res['message']}")
        with self._lock:
            if status == "success":
                self.compressed += 1
                self.bytes_in += res["in_size"]
                self.bytes_out += res["out_size"]
            elif status == "error":
                self.failed += 1
            else:
                self.skipped += 1
            if not quiet:
                self.done_files += 1
                self.done_bytes += job["size"]

    def _video_task(self, job: dict) -> dict:
        key = id(job)
        with self._lock:
            self.active_videos[key] = {"name": job["name"], "kind": "video", "size": job["size"], "pct": 0.0,
                                       "fps": "", "speed": "", "encoder": "", "mode": "", "started": time.monotonic()}

        def report(d):
            with self._lock:
                if key in self.active_videos:
                    self.active_videos[key].update(d)
        try:
            return compress_video(job, self.hw, self.ctl, report)
        finally:
            with self._lock:
                self.active_videos.pop(key, None)

    def _execute(self, images: list, videos: list):
        s = self.s
        work = images + videos
        self.total_files = len(work)
        self.total_bytes = sum(j["size"] for j in work)
        if not work:
            self.log("info", "Nothing new to compress.")
            return
        self.compress_started = time.monotonic()
        cpu = os.cpu_count() or 4
        primary = self.hw.chain(s.vid_codec)[0] if videos and self.hw.encoders else None
        n_vid = 0
        if videos:
            n_vid = max(1, min(s.vid_jobs, 8 if (primary and primary.is_gpu) else 2, len(videos)))
            where = f"{primary.label}" if primary else "no encoder"
            self.log("info", f"Videos: {len(videos)} file(s) on {where}, {n_vid} at a time.")
        n_img = 0
        if images:
            n_img = max(1, min(61, len(images), cpu if not videos else max(2, cpu - 2)))
            self.log("info", f"Images: {len(images)} file(s) across {n_img} CPU worker(s).")

        futures: dict = {}
        vid_pool = ThreadPoolExecutor(n_vid, thread_name_prefix="prism-video") if videos else None
        img_pool = None
        attempts: dict = {}

        def new_img_pool():
            return ProcessPoolExecutor(n_img, mp_context=mp.get_context("spawn"), initializer=_worker_init,
                                       initargs=(self.ctl.mp_cancel, self.ctl.mp_running))

        def submit_images(batch):
            order = []
            for job in batch:
                fut = img_pool.submit(compress_image, job)
                futures[fut] = job
                order.append((fut, job))
            self._img_order, self._img_cursor = order, 0

        try:
            for job in videos:
                futures[vid_pool.submit(self._video_task, job)] = job
            if images:
                img_pool = new_img_pool()
                submit_images(images)
            pending = set(futures)
            while pending:
                done, pending = wait(pending, timeout=0.4, return_when=FIRST_COMPLETED)
                if img_pool is not None:
                    self.ctl.set_worker_pids(list(getattr(img_pool, "_processes", {}) or {}))
                    order = self._img_order
                    while self._img_cursor < len(order) and order[self._img_cursor][0].done():
                        self._img_cursor += 1
                crashed = []
                for fut in done:
                    job = futures.pop(fut)
                    try:
                        res = fut.result()
                    except BrokenProcessPool:
                        crashed.append(job)
                        continue
                    except CancelledError:
                        res = {**_new_result(job), "status": "cancelled", "reason": "cancelled"}
                    except Exception as e:
                        res = {**_new_result(job), "message": f"{type(e).__name__}: {e}",
                               "log": [("error", f"{job['name']}: {type(e).__name__}: {e}")]}
                    self._finish(job, res)
                if crashed and not self.ctl.cancelled:
                    # A worker died (native crash / out of memory). Rebuild the pool and retry
                    # what was in flight; a file that keeps killing the pool is marked failed.
                    retry = []
                    for job in crashed:
                        attempts[job["path"]] = attempts.get(job["path"], 0) + 1
                        if attempts[job["path"]] >= 3:
                            self._finish(job, {**_new_result(job), "message": "repeatedly crashed the image decoder",
                                               "log": [("error", f"{job['name']}: repeatedly crashed the image decoder")]})
                        else:
                            retry.append(job)
                    self.log("warn", f"An image worker crashed; restarting the pool and retrying {len(retry)} file(s).")
                    img_pool.shutdown(wait=False, cancel_futures=True)
                    img_pool = new_img_pool()
                    for fut in [f for f in pending if futures.get(f, {}).get("kind") == "image"]:
                        pending.discard(fut)
                        retry.append(futures.pop(fut))
                    submit_images(retry)
                    pending |= {f for f, _ in self._img_order}
                if self.ctl.cancelled:
                    for fut in pending:
                        fut.cancel()
                    break
        finally:
            if self.ctl.cancelled:
                if img_pool is not None:
                    killer = getattr(img_pool, "kill_workers", None)
                    if killer:
                        try:
                            killer()
                        except Exception:
                            pass
                    else:
                        for p in list((getattr(img_pool, "_processes", None) or {}).values()):
                            try:
                                p.kill()
                            except Exception:
                                pass
                    img_pool.shutdown(wait=False, cancel_futures=True)
                if vid_pool is not None:
                    vid_pool.shutdown(wait=True, cancel_futures=True)
            else:
                if img_pool is not None:
                    img_pool.shutdown(wait=True)
                if vid_pool is not None:
                    vid_pool.shutdown(wait=True)
            self.ctl.set_worker_pids([])
