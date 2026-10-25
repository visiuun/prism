"""Prism desktop UI: CustomTkinter with a pure-black OLED theme.

All worker threads talk to Tk through thread-safe queues drained on the main
thread; nothing outside the main thread ever touches a widget.
"""
from __future__ import annotations

import os
import sys
import json
import time
import queue
import threading
import subprocess
import dataclasses
from datetime import datetime
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image

import prism_engine as engine
from prism_engine import Settings, Session, History, IS_WIN, fmt_bytes, fmt_duration

if IS_WIN:
    import ctypes
    from ctypes import wintypes

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    Observer, FileSystemEventHandler, HAS_WATCHDOG = None, object, False

def resource(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

class C:
    BG = "#000000"
    SURFACE = "#0a0a0b"
    RAISED = "#111113"
    FIELD = "#151518"
    BORDER = "#1c1c20"
    BORDER_HI = "#2b2b31"
    TEXT = "#f5f5f7"
    TEXT_2 = "#a1a1aa"
    TEXT_3 = "#62626b"
    ACCENT = "#ff3b30"
    ACCENT_HOVER = "#ff5d53"
    ACCENT_SOFT = "#200b09"
    GREEN = "#32d74b"
    AMBER = "#ffcc00"
    RED = "#ff6b6b"
    BLUE = "#64d2ff"
    BLUE_SOFT = "#0a1a21"
    R_CARD = 16
    R_CTRL = 10


class F:
    """Fonts, resolved once a Tk root exists (static system fonts first)."""

    @classmethod
    def init(cls, root):
        families = set(tkfont.families(root))

        def pick(*names):
            return next((n for n in names if n in families), "Segoe UI" if IS_WIN else "Helvetica")

        # Tk measures variable fonts (Inter, Segoe UI Variable) too narrow and clips label text, so static fonts go first.
        ui = pick("Segoe UI", "SF Pro Text", "Helvetica Neue", "Ubuntu", "Cantarell", "Inter", "DejaVu Sans")
        display = pick("Segoe UI", "SF Pro Display", "Helvetica Neue", "Ubuntu", "Cantarell", "Inter", "DejaVu Sans")
        mono = pick("Cascadia Mono", "JetBrains Mono", "Consolas", "Menlo", "DejaVu Sans Mono", "Courier New")
        cls.brand = ctk.CTkFont(display, 20, "bold")
        cls.hero = ctk.CTkFont(display, 44, "bold")
        cls.status = ctk.CTkFont(display, 18, "bold")
        cls.h2 = ctk.CTkFont(ui, 14, "bold")
        cls.body = ctk.CTkFont(ui, 13)
        cls.small = ctk.CTkFont(ui, 12)
        cls.tiny = ctk.CTkFont(ui, 11)
        cls.caps = ctk.CTkFont(ui, 10, "bold")
        cls.stat = ctk.CTkFont(display, 24, "bold")
        cls.button = ctk.CTkFont(ui, 13, "bold")
        cls.mono = ctk.CTkFont(mono, 12)


LOG_STYLE = {
    "done": ("DONE", C.GREEN), "skip": ("SKIP", C.TEXT_3), "error": ("FAIL", C.RED), "warn": ("WARN", C.AMBER),
    "info": ("INFO", C.BLUE), "start": ("VID ", C.ACCENT), "muted": ("    ", C.TEXT_3), "head": ("    ", C.TEXT),
}


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

def make_switch(parent, var, command=None) -> ctk.CTkSwitch:
    return ctk.CTkSwitch(parent, text="", variable=var, onvalue=True, offvalue=False, width=46,
                         switch_width=40, switch_height=22, progress_color=C.ACCENT, fg_color=C.BORDER_HI,
                         button_color=C.TEXT, button_hover_color="#ffffff", command=command)


def ghost_button(parent, text, command, width=0, danger=False) -> ctk.CTkButton:
    return ctk.CTkButton(parent, text=text, command=command, height=30, width=width or 28, corner_radius=C.R_CTRL,
                         fg_color=C.FIELD, hover_color="#2a1414" if danger else C.BORDER_HI, border_width=1,
                         border_color=C.BORDER_HI, text_color=C.RED if danger else C.TEXT_2, font=F.small)


def ellipsize(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Card(ctk.CTkFrame):
    def __init__(self, master, **kw):
        super().__init__(master, fg_color=C.SURFACE, corner_radius=C.R_CARD, border_width=1,
                         border_color=C.BORDER, **kw)


class Section(Card):
    """Settings card: title, optional subtitle and master switch, then stacked rows."""

    def __init__(self, master, title, subtitle=None, switch_var=None):
        super().__init__(master)
        self.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 2))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text=title, font=F.h2, text_color=C.TEXT, anchor="w").grid(row=0, column=0, sticky="w")
        if subtitle:
            ctk.CTkLabel(head, text=subtitle, font=F.small, text_color=C.TEXT_3, anchor="w").grid(row=1, column=0, sticky="w")
        self.switch = None
        if switch_var is not None:
            self.switch = make_switch(head, switch_var)
            self.switch.grid(row=0, column=1, rowspan=2, sticky="e")
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="ew", padx=18, pady=(4, 16))
        self.body.grid_columnconfigure(0, weight=1)
        self._row = 0

    def add(self, widget, pady=(10, 0)):
        widget.grid(row=self._row, column=0, sticky="ew", pady=pady)
        self._row += 1
        return widget


class FieldLabel(ctk.CTkLabel):
    def __init__(self, master, text):
        super().__init__(master, text=text.upper(), font=F.caps, text_color=C.TEXT_3, anchor="w")


class ToggleRow(ctk.CTkFrame):
    def __init__(self, master, title, var, hint=None):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text=title, font=F.body, text_color=C.TEXT, anchor="w").grid(row=0, column=0, sticky="w")
        if hint:
            ctk.CTkLabel(self, text=hint, font=F.tiny, text_color=C.TEXT_3, anchor="w", justify="left",
                         wraplength=300).grid(row=1, column=0, sticky="w")
        self.switch = make_switch(self, var)
        self.switch.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))


class Segmented(ctk.CTkFrame):
    """Segmented control bound to a typed variable via (label, value) pairs."""

    def __init__(self, master, var, options, on_change=None):
        super().__init__(master, fg_color="transparent")
        self.var, self.on_change = var, on_change
        self._value = {label: value for label, value in options}
        self._label = {value: label for label, value in options}
        self.seg = ctk.CTkSegmentedButton(
            self, values=[label for label, _ in options], command=self._picked, height=34, corner_radius=C.R_CTRL,
            border_width=3, fg_color=C.FIELD, selected_color=C.ACCENT, selected_hover_color=C.ACCENT_HOVER,
            unselected_color=C.FIELD, unselected_hover_color=C.BORDER_HI, text_color=C.TEXT,
            text_color_disabled=C.TEXT_3, font=F.small, dynamic_resizing=False)
        self.seg.pack(fill="x")
        self._sync()
        var.trace_add("write", lambda *_: self._sync())

    def _sync(self):
        try:
            value = self.var.get()
        except tk.TclError:
            return
        self.seg.set(self._label.get(value, next(iter(self._label.values()))))

    def _picked(self, label):
        self.var.set(self._value[label])
        if self.on_change:
            self.on_change()

    def set_state(self, state):
        on = state == "normal"
        self.seg.configure(state=state, selected_color=C.ACCENT if on else C.BORDER_HI,
                           selected_hover_color=C.ACCENT_HOVER if on else C.BORDER_HI)


class SliderRow(ctk.CTkFrame):
    def __init__(self, master, title, var, lo, hi, describe, hint=None):
        super().__init__(master, fg_color="transparent")
        self.var, self.describe = var, describe
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text=title, font=F.body, text_color=C.TEXT, anchor="w").grid(row=0, column=0, sticky="w")
        self.value = ctk.CTkLabel(self, text="", font=F.small, text_color=C.TEXT, fg_color=C.FIELD,
                                  corner_radius=8, height=24, padx=10)
        self.value.grid(row=0, column=1, sticky="e")
        self.slider = ctk.CTkSlider(self, from_=lo, to=hi, number_of_steps=hi - lo, variable=var, height=18,
                                    command=lambda _v: self._refresh(), progress_color=C.ACCENT, fg_color=C.BORDER_HI,
                                    button_color=C.TEXT, button_hover_color="#ffffff", button_length=0)
        self.slider.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 2))
        if hint:
            ctk.CTkLabel(self, text=hint, font=F.tiny, text_color=C.TEXT_3, anchor="w", justify="left",
                         wraplength=340).grid(row=2, column=0, columnspan=2, sticky="w")
        var.trace_add("write", lambda *_: self._refresh())
        self._refresh()

    def _refresh(self):
        try:
            v = int(round(float(self.var.get())))
        except (tk.TclError, ValueError):
            return
        self.value.configure(text=f"{v}  ·  {self.describe(v)}")


class PathRow(ctk.CTkFrame):
    def __init__(self, master, label, var, browse):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)
        FieldLabel(self, label).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.entry = ctk.CTkEntry(self, textvariable=var, height=36, corner_radius=C.R_CTRL, border_width=1,
                                  fg_color=C.FIELD, border_color=C.BORDER_HI, text_color=C.TEXT, font=F.small,
                                  placeholder_text="Choose a folder…", placeholder_text_color=C.TEXT_3)
        self.entry.grid(row=1, column=0, sticky="ew")
        self.button = ctk.CTkButton(self, text="Browse", width=84, height=36, corner_radius=C.R_CTRL, command=browse,
                                    fg_color=C.RAISED, hover_color=C.BORDER_HI, border_width=1, border_color=C.BORDER_HI,
                                    text_color=C.TEXT, font=F.small)
        self.button.grid(row=1, column=1, padx=(8, 0))


class StatTile(Card):
    def __init__(self, master, label):
        super().__init__(master)
        self.grid_columnconfigure(0, weight=1)
        FieldLabel(self, label).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 0))
        self.value = ctk.CTkLabel(self, text="—", font=F.stat, text_color=C.TEXT, anchor="w")
        self.value.grid(row=1, column=0, sticky="w", padx=16)
        self.sub = ctk.CTkLabel(self, text=" ", font=F.tiny, text_color=C.TEXT_3, anchor="w")
        self.sub.grid(row=2, column=0, sticky="w", padx=16, pady=(0, 12))

    def set(self, value, sub=" ", color=C.TEXT):
        self.value.configure(text=value, text_color=color)
        self.sub.configure(text=sub or " ")


class Chip(ctk.CTkFrame):
    COLORS = {"ok": C.GREEN, "warn": C.AMBER, "off": C.TEXT_3, "busy": C.BLUE, "err": C.RED}

    def __init__(self, master, key):
        super().__init__(master, fg_color=C.RAISED, corner_radius=13, border_width=1, border_color=C.BORDER, height=28)
        self.dot = ctk.CTkLabel(self, text="●", font=F.tiny, text_color=C.TEXT_3, width=10)
        self.dot.pack(side="left", padx=(12, 6))
        ctk.CTkLabel(self, text=key, font=F.tiny, text_color=C.TEXT_3).pack(side="left")
        self.value = ctk.CTkLabel(self, text="…", font=F.tiny, text_color=C.TEXT)
        self.value.pack(side="left", padx=(6, 12))

    def set(self, value, state="ok"):
        self.value.configure(text=value)
        self.dot.configure(text_color=self.COLORS.get(state, C.TEXT_3))


class JobRow(ctk.CTkFrame):
    def __init__(self, master, kind):
        super().__init__(master, fg_color=C.RAISED, corner_radius=12)
        self.kind = kind
        self.grid_columnconfigure(1, weight=1)
        video = kind == "video"
        ctk.CTkLabel(self, text="VID" if video else "IMG", font=F.caps, width=38, height=22, corner_radius=6,
                     fg_color=C.ACCENT_SOFT if video else C.BLUE_SOFT,
                     text_color=C.ACCENT if video else C.BLUE).grid(row=0, column=0, rowspan=2, padx=(12, 10), pady=10)
        self.name = ctk.CTkLabel(self, text="", font=F.small, text_color=C.TEXT, anchor="w")
        self.name.grid(row=0, column=1, sticky="ew", pady=(9, 0))
        self.pct = ctk.CTkLabel(self, text="", font=F.small, text_color=C.TEXT_2, anchor="e", width=48)
        self.pct.grid(row=0, column=2, padx=(8, 14), pady=(9, 0), sticky="e")
        self.detail = ctk.CTkLabel(self, text="", font=F.tiny, text_color=C.TEXT_3, anchor="w")
        self.detail.grid(row=1, column=1, columnspan=2, sticky="ew")
        self.bar = ctk.CTkProgressBar(self, height=4, corner_radius=2, fg_color=C.BORDER,
                                      progress_color=C.ACCENT if video else C.BLUE,
                                      mode="determinate" if video else "indeterminate")
        self.bar.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(0, 14), pady=(4, 11))
        if video:
            self.bar.set(0)
        else:
            self.bar.start()

    def update_from(self, d: dict):
        self.name.configure(text=ellipsize(d["name"], 58))
        if self.kind == "video":
            pct = d.get("pct", 0.0)
            self.bar.set(pct)
            self.pct.configure(text=f"{pct:.0%}")
            bits = [d.get("encoder", ""), d.get("mode", "")]
            try:
                fps = float(d.get("fps") or 0)
                if fps > 0:
                    bits.append(f"{fps:.0f} fps")
            except ValueError:
                pass
            try:
                speed = float((d.get("speed") or "").strip().rstrip("x"))
                if speed >= 0.1:
                    bits.append(f"{speed:.1f}× realtime")
            except ValueError:
                pass
            self.detail.configure(text="  ·  ".join(b for b in bits if b) or "Starting…")
        else:
            self.detail.configure(text=f"Encoding · {fmt_bytes(d.get('size', 0))}")

    def destroy(self):
        try:
            self.bar.stop()
        except Exception:
            pass
        super().destroy()


# ---------------------------------------------------------------------------
# Platform integration
# ---------------------------------------------------------------------------

class TaskbarProgress:
    """ITaskbarList3 via raw COM vtable calls (main thread only)."""
    NOPROGRESS, INDETERMINATE, NORMAL, ERROR, PAUSED = 0, 0x1, 0x2, 0x4, 0x8

    def __init__(self):
        self.ptr = None
        if not IS_WIN:
            return
        try:
            ctypes.windll.ole32.CoInitialize(0)

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                            ("Data4", ctypes.c_ubyte * 8)]

            def guid(s):
                g = GUID()
                ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
                return g

            class Vtbl(ctypes.Structure):
                _fields_ = [("QueryInterface", ctypes.c_void_p), ("AddRef", ctypes.c_void_p), ("Release", ctypes.c_void_p),
                            ("HrInit", ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)),
                            ("AddTab", ctypes.c_void_p), ("DeleteTab", ctypes.c_void_p), ("ActivateTab", ctypes.c_void_p),
                            ("SetActiveAlt", ctypes.c_void_p), ("MarkFullscreenWindow", ctypes.c_void_p),
                            ("SetProgressValue", ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND,
                                                                    ctypes.c_ulonglong, ctypes.c_ulonglong)),
                            ("SetProgressState", ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND, ctypes.c_int))]

            class ITaskbarList3(ctypes.Structure):
                _fields_ = [("lpVtbl", ctypes.POINTER(Vtbl))]

            ptr = ctypes.POINTER(ITaskbarList3)()
            if ctypes.windll.ole32.CoCreateInstance(ctypes.byref(guid("{56FDF344-FD6D-11d0-958A-006097C9A090}")), 0, 1,
                                                    ctypes.byref(guid("{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}")),
                                                    ctypes.byref(ptr)) == 0:
                ptr.contents.lpVtbl.contents.HrInit(ptr)
                self.ptr = ptr
        except Exception:
            self.ptr = None

    def state(self, hwnd, state):
        if self.ptr and hwnd:
            try:
                self.ptr.contents.lpVtbl.contents.SetProgressState(self.ptr, hwnd, state)
            except Exception:
                pass

    def value(self, hwnd, done, total):
        if self.ptr and hwnd and total > 0:
            try:
                self.ptr.contents.lpVtbl.contents.SetProgressValue(self.ptr, hwnd, int(done), int(total))
            except Exception:
                pass


def keep_awake(on: bool):
    if IS_WIN:
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def style_window(win):
    """Prism icon, plus a pure-black caption bar on Windows 11 (DWM dark mode, caption and border colour)."""
    if not IS_WIN:
        try:
            icon = tk.PhotoImage(file=resource("prism.png"))
            win.iconphoto(True, icon)
            win._prism_icon = icon          # Tk drops the image once Python lets go of it
        except Exception:
            pass
        return
    try:
        win.iconbitmap(resource("prism.ico"))
    except Exception:
        pass
    try:
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
    except Exception:
        return
    for attr, value in ((20, 1), (35, 0x000000), (34, 0x00201C1C)):
        try:
            v = ctypes.c_int(value)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
        except Exception:
            pass


def open_path(path: str):
    try:
        if IS_WIN:
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Watch folder
# ---------------------------------------------------------------------------

class _WatchHandler(FileSystemEventHandler):
    def __init__(self, service):
        self.service = service

    def on_created(self, event):
        if not event.is_directory:
            self.service.offer(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self.service.offer(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self.service.offer(event.dest_path)


class WatchService:
    """Compresses files as they land in a folder, once they stop growing."""

    SETTLE_SECONDS = 3.0

    def __init__(self, app, watch_dir: str, out_dir: str):
        self.app = app
        self.watch_dir = os.path.abspath(watch_dir)
        self.out_dir = os.path.abspath(out_dir)
        self._out_norm = os.path.normcase(self.out_dir)
        self._pending = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.ctl = engine.Control()
        self.observer = None
        self.thread = None

    @property
    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        self.observer = Observer()
        self.observer.schedule(_WatchHandler(self), self.watch_dir, recursive=True)
        self.observer.start()
        self.thread = threading.Thread(target=self._loop, name="prism-watch", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        self.ctl.cancel()
        if self.observer:
            try:
                self.observer.stop()
                self.observer.join(3)
            except Exception:
                pass

    def offer(self, path: str):
        norm = os.path.normcase(os.path.abspath(path))
        name = os.path.basename(path)
        ext = os.path.splitext(name)[1].lower()
        if name.startswith(engine.TEMP_PREFIX) or (ext not in engine.IMAGE_EXTS and ext not in engine.VIDEO_EXTS):
            return
        if norm == self._out_norm or norm.startswith(self._out_norm + os.sep):
            return
        rel_parts = os.path.relpath(path, self.watch_dir).split(os.sep)
        if any(p in engine.SKIP_DIRS for p in rel_parts[:-1]):
            return
        with self._lock:
            self._pending[path] = (time.monotonic(), -1)

    def _loop(self):
        history = History(self.app.history_path)
        log = self.app.log_threadsafe
        try:
            while not self._stop.wait(1.0):
                ready, now = [], time.monotonic()
                with self._lock:
                    for path, (stamp, last_size) in list(self._pending.items()):
                        if now - stamp < self.SETTLE_SECONDS:
                            continue
                        try:
                            size = os.path.getsize(path)
                        except OSError:
                            del self._pending[path]
                            continue
                        if size != last_size:          # still being written
                            self._pending[path] = (now, size)
                            continue
                        del self._pending[path]
                        ready.append(path)
                for path in ready:
                    if self._stop.is_set():
                        break
                    settings, hw = self.app.settings_snapshot(), self.app.hw
                    if hw is None or self.app.batch_running:
                        with self._lock:
                            self._pending[path] = (time.monotonic(), -1)
                        continue
                    try:
                        with open(path, "rb"):
                            pass
                    except OSError:
                        with self._lock:
                            self._pending[path] = (time.monotonic(), -1)
                        continue
                    kind_on = settings.images if os.path.splitext(path)[1].lower() in engine.IMAGE_EXTS else settings.videos
                    if not kind_on:
                        continue
                    settings = dataclasses.replace(settings, input_dir=self.watch_dir, output_dir=self.out_dir)
                    log("info", f"Watch: {os.path.basename(path)}")
                    try:
                        engine.process_single(path, self.watch_dir, settings, hw, history, self.ctl, log)
                    except Exception as e:
                        log("error", f"Watch: {os.path.basename(path)}: {type(e).__name__}: {e}")
        finally:
            history.close()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

IMAGE_FORMAT_HINTS = {
    "webp": "Best all-rounder: small files that open everywhere.",
    "jxl": "Smallest at high quality. Newer format; check your viewers support it.",
    "jpeg": "Maximum compatibility. Transparent images fall back to WebP.",
    "original": "Keep each file's own format (PNG stays lossless).",
}
CONFIG_DEFAULTS = dataclasses.asdict(Settings())


def describe_image_quality(q):
    return "Near-lossless" if q >= 90 else "High" if q >= 80 else "Balanced" if q >= 65 else "Compact" if q >= 50 else "Tiny"


def describe_video_quality(q):
    return "Archival" if q <= 22 else "High" if q <= 26 else "Balanced" if q <= 30 else "Compact" if q <= 34 else "Tiny"


class PrismApp(ctk.CTk):
    def __init__(self):
        ctk.set_appearance_mode("dark")
        super().__init__(fg_color=C.BG)
        F.init(self)
        self.title("Prism")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{min(1260, sw - 60)}x{min(840, sh - 100)}")
        self.minsize(min(1080, sw - 60), min(700, sh - 100))
        self.data_dir = engine.app_data_dir()
        self.config_path = os.path.join(self.data_dir, "config.json")
        self.history_path = os.path.join(self.data_dir, "prism_log.db")
        self.log_dir = os.path.join(self.data_dir, "logs")
        os.makedirs(self.log_dir, exist_ok=True)
        History(self.history_path).close()   # create / migrate schema up front

        self.hw = None
        self.session = None
        self.session_thread = None
        self.watch = None
        self.taskbar = TaskbarProgress()
        self._events = queue.SimpleQueue()
        self._logs = queue.SimpleQueue()
        self._log_file = None
        self._log_file_day = None
        self._job_rows = {}
        self._controls = []
        self._save_after = None
        self._finish_handled = False

        self.config_data = self._load_config()
        self._make_vars()
        self._build()
        self._settings_cache = self._collect_settings()
        for var in self._vars.values():
            var.trace_add("write", self._on_setting_changed)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(60, self._pump)
        self.after(120, lambda: style_window(self))
        self._set_idle_view()
        self._prune_logs()
        threading.Thread(target=self._detect_hardware, name="prism-detect", daemon=True).start()

    # -- config --------------------------------------------------------------
    def _load_config(self) -> dict:
        try:
            with open(self.config_path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_config(self):
        self._save_after = None
        s = self._collect_settings()
        data = dict(self.config_data)
        data.update({
            "last_input": s.input_dir, "last_output": s.output_dir, "process_images": s.images,
            "process_videos": s.videos, "img_format": s.img_format, "img_quality": s.img_quality,
            "img_max_dim": s.img_max_dim, "quality_guard": s.quality_guard, "vid_codec": s.vid_codec,
            "vid_quality": s.vid_quality, "vid_max_height": s.vid_max_height, "vid_fps_cap": s.vid_fps_cap,
            "vid_jobs": s.vid_jobs, "preserve_metadata": s.preserve_metadata, "scan_subfolders": s.scan_subfolders,
            "append_hash": s.append_hash, "copy_on_skip": s.copy_on_skip, "move_to_processed": s.move_processed,
            "move_to_failed": s.move_failed, "config_version": 3,
        })
        data.pop("downscale_1080p", None)
        data.pop("console_state", None)
        self.config_data = data
        tmp = self.config_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.config_path)
        except OSError as e:
            self.log("warn", f"Could not save settings: {e}")

    def _make_vars(self):
        d, c = CONFIG_DEFAULTS, self.config_data
        # Prism 2.x stored "originalformat" and a combined 1080p/45 fps toggle; migrate both.
        fmt = str(c.get("img_format", "webp")).lower().replace(" ", "")
        fmt = "original" if fmt.startswith("original") else fmt if fmt in ("webp", "jpeg", "jxl") else "webp"
        max_h = c.get("vid_max_height", 1080 if c.get("downscale_1080p") else 0)
        self._vars = {
            "input_dir": tk.StringVar(value=c.get("last_input", "")),
            "output_dir": tk.StringVar(value=c.get("last_output", "")),
            "images": tk.BooleanVar(value=bool(c.get("process_images", True))),
            "videos": tk.BooleanVar(value=bool(c.get("process_videos", True))),
            "img_format": tk.StringVar(value=fmt),
            "img_quality": tk.IntVar(value=max(30, min(95, int(c.get("img_quality", d["img_quality"]))))),
            "img_max_dim": tk.IntVar(value=int(c.get("img_max_dim", 0))),
            "quality_guard": tk.BooleanVar(value=bool(c.get("quality_guard", True))),
            "vid_codec": tk.StringVar(value=c.get("vid_codec", "auto")),
            "vid_quality": tk.IntVar(value=max(18, min(40, int(c.get("vid_quality", d["vid_quality"]))))),
            "vid_max_height": tk.IntVar(value=int(max_h)),
            "vid_fps_cap": tk.BooleanVar(value=bool(c.get("vid_fps_cap", False))),
            "vid_jobs": tk.IntVar(value=max(1, min(8, int(c.get("vid_jobs", d["vid_jobs"]))))),
            "preserve_metadata": tk.BooleanVar(value=bool(c.get("preserve_metadata", True))),
            "scan_subfolders": tk.BooleanVar(value=bool(c.get("scan_subfolders", True))),
            "append_hash": tk.BooleanVar(value=bool(c.get("append_hash", True))),
            "copy_on_skip": tk.BooleanVar(value=bool(c.get("copy_on_skip", False))),
            "move_processed": tk.BooleanVar(value=bool(c.get("move_to_processed", False))),
            "move_failed": tk.BooleanVar(value=bool(c.get("move_to_failed", True))),
            "overwrite": tk.BooleanVar(value=False),      # never saved between sessions
        }
        self.shutdown_var = tk.BooleanVar(value=False)    # never persisted

    def _collect_settings(self) -> Settings:
        v = self._vars

        def num(name, fallback):
            try:
                return int(round(float(v[name].get())))
            except (tk.TclError, ValueError):
                return fallback

        return Settings(
            input_dir=v["input_dir"].get().strip(), output_dir=v["output_dir"].get().strip(),
            images=v["images"].get(), videos=v["videos"].get(), img_format=v["img_format"].get(),
            img_quality=num("img_quality", 75), img_max_dim=num("img_max_dim", 0),
            quality_guard=v["quality_guard"].get(), vid_codec=v["vid_codec"].get(),
            vid_quality=num("vid_quality", 28), vid_max_height=num("vid_max_height", 0),
            vid_fps_cap=v["vid_fps_cap"].get(), vid_jobs=num("vid_jobs", 2),
            preserve_metadata=v["preserve_metadata"].get(), scan_subfolders=v["scan_subfolders"].get(),
            append_hash=v["append_hash"].get(), copy_on_skip=v["copy_on_skip"].get(),
            move_processed=v["move_processed"].get(), move_failed=v["move_failed"].get(),
            overwrite=v["overwrite"].get())

    def settings_snapshot(self) -> Settings:
        return dataclasses.replace(self._settings_cache)

    def _on_setting_changed(self, *_):
        self._settings_cache = self._collect_settings()
        self._img_hint.configure(text=IMAGE_FORMAT_HINTS.get(self._vars["img_format"].get(), ""))
        self._refresh_codec_hint()
        if self._save_after:
            self.after_cancel(self._save_after)
        self._save_after = self.after(600, self._save_config)

    # -- layout --------------------------------------------------------------
    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_header()
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 20))
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)
        self._build_settings(body)
        self._build_dashboard(body)

    def _build_header(self):
        bar = ctk.CTkFrame(self, fg_color="transparent", height=64)
        bar.grid(row=0, column=0, sticky="ew", padx=24, pady=(14, 12))
        bar.grid_columnconfigure(1, weight=1)
        brand = ctk.CTkFrame(bar, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="w")
        try:
            logo = ctk.CTkImage(Image.open(resource("prism.png")), size=(30, 30))
            ctk.CTkLabel(brand, image=logo, text="").pack(side="left", padx=(0, 10))
        except Exception:
            pass
        ctk.CTkLabel(brand, text="Prism", font=F.brand, text_color=C.TEXT).pack(side="left")
        ctk.CTkLabel(brand, text=f"v{engine.APP_VERSION}", font=F.tiny, text_color=C.TEXT_3).pack(side="left", padx=(8, 0), pady=(6, 0))
        chips = ctk.CTkFrame(bar, fg_color="transparent")
        chips.grid(row=0, column=2, sticky="e")
        self.chip_gpu = Chip(chips, "GPU")
        self.chip_enc = Chip(chips, "Encoder")
        self.chip_ff = Chip(chips, "FFmpeg")
        for chip in (self.chip_gpu, self.chip_enc, self.chip_ff):
            chip.pack(side="left", padx=(8, 0))
            chip.set("detecting…", "busy")

    def _build_settings(self, parent):
        panel = ctk.CTkScrollableFrame(parent, width=410, fg_color="transparent", corner_radius=0,
                                       scrollbar_button_color=C.BORDER, scrollbar_button_hover_color=C.BORDER_HI)
        panel.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        panel.grid_columnconfigure(0, weight=1)
        v = self._vars
        row = 0

        def place(widget, pady=(0, 14)):
            nonlocal row
            widget.grid(row=row, column=0, sticky="ew", pady=pady, padx=(0, 6))
            row += 1

        folders = Section(panel, "Folders", "Originals are never modified or deleted.")
        self._in_row = folders.add(PathRow(folders.body, "Source", v["input_dir"], self._browse_input), pady=(6, 0))
        self._out_row = folders.add(PathRow(folders.body, "Destination", v["output_dir"], self._browse_output), pady=(12, 0))
        place(folders)

        images = Section(panel, "Images", None, v["images"])
        images.add(FieldLabel(images.body, "Format"), pady=(4, 0))
        self._img_fmt = images.add(Segmented(images.body, v["img_format"],
                                             [("WebP", "webp"), ("JPEG XL", "jxl"), ("JPEG", "jpeg"), ("Original", "original")]), pady=(6, 0))
        self._img_hint = images.add(ctk.CTkLabel(images.body, text=IMAGE_FORMAT_HINTS.get(v["img_format"].get(), ""),
                                                 font=F.tiny, text_color=C.TEXT_3, anchor="w"), pady=(4, 0))
        self._img_q = images.add(SliderRow(images.body, "Quality", v["img_quality"], 30, 95, describe_image_quality,
                                           "Applied exactly as set. 75–85 is visually lossless for most photos."), pady=(14, 0))
        images.add(FieldLabel(images.body, "Max resolution"), pady=(14, 0))
        self._img_dim = images.add(Segmented(images.body, v["img_max_dim"],
                                             [("Original", 0), ("4K", 3840), ("1440p", 2560), ("1080p", 1920)]), pady=(6, 0))
        self._guard = images.add(ToggleRow(images.body, "Quality guard", v["quality_guard"],
                                           "Scores every result against the original (SSIM on its most detailed areas) "
                                           "and re-encodes higher if fine detail was lost."), pady=(14, 0))
        place(images)

        videos = Section(panel, "Videos", None, v["videos"])
        videos.add(FieldLabel(videos.body, "Codec"), pady=(4, 0))
        self._vid_codec = videos.add(Segmented(videos.body, v["vid_codec"],
                                               [("Auto", "auto"), ("AV1", "av1"), ("HEVC", "hevc"), ("H.264", "h264")]), pady=(6, 0))
        self._codec_hint = videos.add(ctk.CTkLabel(videos.body, text="Detecting encoders…", font=F.tiny,
                                                   text_color=C.TEXT_3, anchor="w"), pady=(4, 0))
        self._vid_q = videos.add(SliderRow(videos.body, "Quality (CQ)", v["vid_quality"], 18, 40, describe_video_quality,
                                           "Lower = better. Constant quality: bitrate follows the content, no caps. Default 28."), pady=(14, 0))
        videos.add(FieldLabel(videos.body, "Max resolution"), pady=(14, 0))
        self._vid_res = videos.add(Segmented(videos.body, v["vid_max_height"],
                                             [("Original", 0), ("4K", 2160), ("1440p", 1440), ("1080p", 1080), ("720p", 720)]), pady=(6, 0))
        self._fps = videos.add(ToggleRow(videos.body, "Limit to 30 fps", v["vid_fps_cap"],
                                         "Drops whole frames only (60→30, 50→25) so motion stays smooth."), pady=(14, 0))
        self._jobs = videos.add(SliderRow(videos.body, "Parallel videos", v["vid_jobs"], 1, 8,
                                          lambda n: "GPU sessions" if n > 1 else "one at a time"), pady=(14, 0))
        place(videos)

        general = Section(panel, "Behaviour")
        toggles = [
            ("Preserve metadata", "preserve_metadata", "EXIF, GPS, XMP, chapters and file dates. Colour profiles are always kept."),
            ("Scan subfolders", "scan_subfolders", None),
            ("Add content hash to names", "append_hash", "photo_1a2b3c4d.webp: unique names, safe re-runs."),
            ("Copy originals that can't shrink", "copy_on_skip", "So the destination always holds a complete set."),
            ("Move originals to PROCESSED", "move_processed", None),
            ("Move failures to FAILED", "move_failed", None),
            ("Re-compress everything", "overwrite", "Ignore history for this session only."),
        ]
        self._toggles = []
        for i, (title, key, hint) in enumerate(toggles):
            self._toggles.append(general.add(ToggleRow(general.body, title, v[key], hint), pady=(4 if i == 0 else 12, 0)))
        self._toggles.append(general.add(ToggleRow(general.body, "Shut down when finished", self.shutdown_var), pady=(12, 0)))
        actions = ctk.CTkFrame(general.body, fg_color="transparent")
        actions.grid_columnconfigure((0, 1), weight=1)
        self._reset_btn = ghost_button(actions, "Reset history", self._clear_history, danger=True)
        self._reset_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ghost_button(actions, "Watch folder", self._open_watch).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        general.add(actions, pady=(18, 0))
        place(general, pady=(0, 4))

        self._controls = [self._in_row.entry, self._in_row.button, self._out_row.entry, self._out_row.button,
                          images.switch, videos.switch, self._img_q.slider, self._vid_q.slider, self._jobs.slider,
                          self._guard.switch, self._fps.switch, self._reset_btn] + [t.switch for t in self._toggles[:-1]]
        self._segments = [self._img_fmt, self._img_dim, self._vid_codec, self._vid_res]

    def _build_dashboard(self, parent):
        dash = ctk.CTkFrame(parent, fg_color="transparent")
        dash.grid(row=0, column=1, sticky="nsew")
        dash.grid_columnconfigure(0, weight=1)
        dash.grid_rowconfigure(3, weight=1)

        hero = Card(dash)
        hero.grid(row=0, column=0, sticky="ew")
        hero.grid_columnconfigure(0, weight=1)
        top = ctk.CTkFrame(hero, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=22, pady=(20, 0))
        top.grid_columnconfigure(0, weight=1)
        self.status_title = ctk.CTkLabel(top, text="Ready", font=F.status, text_color=C.TEXT, anchor="w")
        self.status_title.grid(row=0, column=0, sticky="w")
        self.status_sub = ctk.CTkLabel(top, text="", font=F.small, text_color=C.TEXT_2, anchor="w")
        self.status_sub.grid(row=1, column=0, sticky="w")
        buttons = ctk.CTkFrame(top, fg_color="transparent")
        buttons.grid(row=0, column=1, rowspan=2, sticky="e")
        self.start_btn = ctk.CTkButton(buttons, text="Start", width=128, height=42, corner_radius=12, font=F.button,
                                       fg_color=C.ACCENT, hover_color=C.ACCENT_HOVER, text_color="#ffffff",
                                       text_color_disabled="#7a7a82", command=self._start)
        self.start_btn.pack(side="left")
        self.pause_btn = ctk.CTkButton(buttons, text="Pause", width=96, height=42, corner_radius=12, font=F.button,
                                       fg_color=C.FIELD, hover_color=C.BORDER_HI, border_width=1, border_color=C.BORDER_HI,
                                       text_color=C.TEXT, text_color_disabled=C.TEXT_3, command=self._toggle_pause)
        self.pause_btn.pack(side="left", padx=(8, 0))
        self.stop_btn = ctk.CTkButton(buttons, text="Stop", width=84, height=42, corner_radius=12, font=F.button,
                                      fg_color=C.FIELD, hover_color="#2a1414", border_width=1, border_color=C.BORDER_HI,
                                      text_color=C.RED, text_color_disabled=C.TEXT_3, command=self._stop)
        self.stop_btn.pack(side="left", padx=(8, 0))

        mid = ctk.CTkFrame(hero, fg_color="transparent")
        mid.grid(row=1, column=0, sticky="ew", padx=22, pady=(10, 0))
        mid.grid_columnconfigure(1, weight=1)
        self.percent = ctk.CTkLabel(mid, text="0%", font=F.hero, text_color=C.TEXT, anchor="w")
        self.percent.grid(row=0, column=0, sticky="sw")
        self.meta = ctk.CTkLabel(mid, text="", font=F.small, text_color=C.TEXT_3, anchor="e")
        self.meta.grid(row=0, column=1, sticky="se", pady=(0, 10))
        self.progress = ctk.CTkProgressBar(hero, height=8, corner_radius=4, fg_color=C.BORDER, progress_color=C.ACCENT)
        self.progress.set(0)
        self.progress.grid(row=2, column=0, sticky="ew", padx=22, pady=(6, 22))

        stats = ctk.CTkFrame(dash, fg_color="transparent")
        stats.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        stats.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="stat")
        self.tile_done = StatTile(stats, "Compressed")
        self.tile_saved = StatTile(stats, "Space saved")
        self.tile_skip = StatTile(stats, "Skipped")
        self.tile_fail = StatTile(stats, "Failed")
        for i, tile in enumerate((self.tile_done, self.tile_saved, self.tile_skip, self.tile_fail)):
            tile.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 7, 0 if i == 3 else 7))

        active = Card(dash)
        active.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        active.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(active, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 6))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="Now processing", font=F.h2, text_color=C.TEXT).grid(row=0, column=0, sticky="w")
        self.active_count = ctk.CTkLabel(head, text="", font=F.small, text_color=C.TEXT_3)
        self.active_count.grid(row=0, column=1, sticky="e")
        self.jobs_frame = ctk.CTkScrollableFrame(active, height=168, fg_color="transparent", corner_radius=0,
                                                 scrollbar_button_color=C.BORDER, scrollbar_button_hover_color=C.BORDER_HI)
        self.jobs_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 12))
        self.jobs_frame.grid_columnconfigure(0, weight=1)
        self.jobs_empty = ctk.CTkLabel(self.jobs_frame, text="Active files show up here while compressing.",
                                       font=F.small, text_color=C.TEXT_3)
        self.jobs_empty.grid(row=0, column=0, pady=58)

        logcard = Card(dash)
        logcard.grid(row=3, column=0, sticky="nsew", pady=(14, 0))
        logcard.grid_columnconfigure(0, weight=1)
        logcard.grid_rowconfigure(1, weight=1)
        lhead = ctk.CTkFrame(logcard, fg_color="transparent")
        lhead.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 4))
        lhead.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(lhead, text="Activity", font=F.h2, text_color=C.TEXT).grid(row=0, column=0, sticky="w")
        ghost_button(lhead, "Open logs", lambda: open_path(self.log_dir), width=90).grid(row=0, column=1, padx=(0, 6))
        ghost_button(lhead, "Clear", self._clear_log, width=64).grid(row=0, column=2)
        self.logbox = ctk.CTkTextbox(logcard, fg_color=C.SURFACE, text_color=C.TEXT_2, font=F.mono, wrap="word",
                                     border_width=0, corner_radius=0, activate_scrollbars=True,
                                     scrollbar_button_color=C.BORDER, scrollbar_button_hover_color=C.BORDER_HI)
        self.logbox.grid(row=1, column=0, sticky="nsew", padx=(12, 6), pady=(0, 12))
        self.logbox.tag_config("time", foreground=C.TEXT_3)
        for level, (_, color) in LOG_STYLE.items():
            self.logbox.tag_config(level, foreground=color)
        self.logbox.tag_config("errtext", foreground=C.RED)
        self.logbox.tag_config("headtext", foreground=C.TEXT)
        self.logbox.configure(state="disabled")

    # -- thread-safe plumbing -------------------------------------------------
    def post(self, fn, *args):
        self._events.put((fn, args))

    def log_threadsafe(self, level: str, msg: str):
        self._logs.put((datetime.now(), level, msg))

    log = log_threadsafe

    def _pump(self):
        try:
            for _ in range(200):
                fn, args = self._events.get_nowait()
                try:
                    fn(*args)
                except Exception as e:
                    self.log("error", f"UI error: {type(e).__name__}: {e}")
        except queue.Empty:
            pass
        self._flush_logs()
        self.after(60, self._pump)

    def _flush_logs(self):
        batch = []
        try:
            while len(batch) < 500:
                batch.append(self._logs.get_nowait())
        except queue.Empty:
            pass
        if not batch:
            return
        at_bottom = self.logbox.yview()[1] > 0.995
        self.logbox.configure(state="normal")
        file_lines = []
        for stamp, level, msg in batch:
            badge, _ = LOG_STYLE.get(level, LOG_STYLE["info"])
            t = stamp.strftime("%H:%M:%S")
            self.logbox.insert("end", f"{t}  ", "time")
            self.logbox.insert("end", f"{badge}  ", level)
            text_tag = "errtext" if level == "error" else "headtext" if level == "head" else ()
            self.logbox.insert("end", msg + "\n", text_tag)
            file_lines.append(f"{stamp:%Y-%m-%d %H:%M:%S} {badge.strip() or '-':5} {msg}\n")
        lines = int(self.logbox.index("end-1c").split(".")[0])
        if lines > 5000:
            self.logbox.delete("1.0", f"{lines - 4000}.0")
        self.logbox.configure(state="disabled")
        if at_bottom:
            self.logbox.see("end")
        self._write_log_file(file_lines)

    def _write_log_file(self, lines):
        day = datetime.now().strftime("%Y-%m-%d")
        try:
            if self._log_file is None or self._log_file_day != day:
                if self._log_file:
                    self._log_file.close()
                self._log_file = open(os.path.join(self.log_dir, f"prism-{day}.log"), "a", encoding="utf-8")
                self._log_file_day = day
            self._log_file.writelines(lines)
            self._log_file.flush()
        except OSError:
            pass

    def _prune_logs(self):
        cutoff = time.time() - 14 * 86400
        try:
            for name in os.listdir(self.log_dir):
                path = os.path.join(self.log_dir, name)
                if name.startswith("prism-") and os.path.getmtime(path) < cutoff:
                    os.remove(path)
        except OSError:
            pass

    def _clear_log(self):
        self.logbox.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.configure(state="disabled")

    # -- hardware ------------------------------------------------------------
    def _detect_hardware(self):
        hw = engine.detect_hardware(self.log_threadsafe)
        self.post(self._hardware_ready, hw)

    def _hardware_ready(self, hw):
        self.hw = hw
        gpu = hw.gpu_name.replace("NVIDIA GeForce ", "").replace("NVIDIA ", "") if hw.gpu_name else "none"
        self.chip_gpu.set(gpu, "ok" if hw.gpu_name else "off")
        primary = hw.primary
        if primary:
            self.chip_enc.set(primary.label.replace(" · ", " "), "ok" if primary.is_gpu else "warn")
        else:
            self.chip_enc.set("unavailable", "err")
        self.chip_ff.set(hw.ffmpeg_version.split("-")[0] if hw.ffmpeg else "missing", "ok" if hw.ffmpeg else "err")
        self._refresh_codec_hint()

        self.log("head", f"Prism {engine.APP_VERSION}  ·  {hw.cpu_name} ({hw.cpu_threads} threads)  ·  "
                         f"{hw.ram_total_gb:.0f} GB RAM ({hw.ram_free_gb:.0f} GB free)")
        if hw.gpu_name:
            vram = f"  ·  {hw.vram_gb:.0f} GB VRAM" if hw.vram_gb else ""
            drv = f"  ·  driver {hw.gpu_driver}" if hw.gpu_driver else ""
            self.log("head", f"GPU  {hw.gpu_name}{drv}{vram}")
        if primary:
            pipe = "  ·  zero-copy CUDA pipeline (NVDEC → NVENC)" if (hw.cuda and primary.vendor == "nvidia") else ""
            depth = "10-bit" if primary.ten_bit else "8-bit"
            others = ", ".join(e.name for e in hw.chain()[1:4])
            self.log("head", f"Video  {primary.label} ({depth}){pipe}")
            if others:
                self.log("muted", f"Fallbacks: {others}")
        elif hw.ffmpeg:
            self.log("warn", "FFmpeg found but no working video encoder; videos will fail.")
        if hw.ffmpeg:
            self.log("muted", f"FFmpeg {hw.ffmpeg_version}  ·  {hw.ffmpeg}")
        else:
            self.log("error", "FFmpeg not found and download failed. Images still work; install FFmpeg for videos.")
        formats = ["WebP", "JPEG"] + (["JPEG XL"] if engine.HAS_JXL else []) + (["HEIC in"] if engine.HAS_HEIF else [])
        self.log("muted", f"Images  {' · '.join(formats)}  ·  {hw.cpu_threads} parallel workers")
        self._set_idle_view()
        if self.config_data.get("autostart") and HAS_WATCHDOG:
            w, o = self.config_data.get("watch_path", ""), self.config_data.get("output_path", "")
            if os.path.isdir(w) and os.path.isdir(o):
                self.start_watch(w, o)

    def _refresh_codec_hint(self):
        if not hasattr(self, "_codec_hint"):
            return
        if self.hw is None:
            self._codec_hint.configure(text="Detecting encoders…")
            return
        chain = self.hw.chain(self._vars["vid_codec"].get())
        if not chain:
            self._codec_hint.configure(text="No working encoder: install FFmpeg to compress videos.", text_color=C.AMBER)
            return
        enc = chain[0]
        pref = self._vars["vid_codec"].get()
        note = "" if pref in ("auto", enc.family) else f" ({pref.upper()} unavailable)"
        self._codec_hint.configure(text=f"Uses {enc.label}{' · 10-bit' if enc.ten_bit else ''}{note}",
                                   text_color=C.TEXT_3 if enc.is_gpu else C.AMBER)

    # -- views ---------------------------------------------------------------
    @property
    def batch_running(self) -> bool:
        return self.session is not None and not self.session.finished

    def _set_controls_state(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for w in self._controls:
            try:
                w.configure(state=state)
            except Exception:
                pass
        for seg in self._segments:
            seg.set_state(state)

    def _set_idle_view(self):
        ready = self.hw is not None
        self.start_btn.configure(state="normal" if ready else "disabled", text="Start" if ready else "Detecting…",
                                 fg_color=C.ACCENT if ready else C.FIELD)
        self.pause_btn.configure(state="disabled", text="Pause")
        self.stop_btn.configure(state="disabled")
        self._set_controls_state(True)
        if not self.session:
            self.status_title.configure(text="Ready" if ready else "Starting up")
            self.status_sub.configure(text="Pick a source and destination, then press Start." if ready
                                      else "Detecting GPU, encoders and FFmpeg…")

    def _set_running_view(self):
        self.start_btn.configure(state="disabled", text="Running", fg_color=C.FIELD)
        self.pause_btn.configure(state="normal", text="Pause")
        self.stop_btn.configure(state="normal")
        self._set_controls_state(False)

    # -- folder pickers --------------------------------------------------------
    def _browse_input(self):
        path = filedialog.askdirectory(title="Choose the folder to compress", initialdir=self._vars["input_dir"].get() or None)
        if path:
            self._vars["input_dir"].set(os.path.normpath(path))

    def _browse_output(self):
        path = filedialog.askdirectory(title="Choose where compressed files go", initialdir=self._vars["output_dir"].get() or None)
        if path:
            self._vars["output_dir"].set(os.path.normpath(path))

    # -- session lifecycle ---------------------------------------------------
    def _validate(self, s: Settings):
        if not s.images and not s.videos:
            return "Turn on Images and/or Videos first."
        if not s.input_dir or not os.path.isdir(s.input_dir):
            return "The source folder doesn't exist."
        if not s.output_dir:
            return "Choose a destination folder."
        src, dst = os.path.normcase(os.path.abspath(s.input_dir)), os.path.normcase(os.path.abspath(s.output_dir))
        if src == dst:
            return "Source and destination must be different folders."
        if src.startswith(dst + os.sep):
            return "The source folder can't be inside the destination folder."
        return None

    def _start(self):
        if self.batch_running or self.hw is None:
            return
        s = self._collect_settings()
        problem = self._validate(s)
        if problem:
            messagebox.showwarning("Prism", problem, parent=self)
            return
        try:
            os.makedirs(s.output_dir, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Prism", f"Cannot create the destination folder:\n{e}", parent=self)
            return
        if s.videos and not self.hw.encoders:
            if not messagebox.askyesno("Prism", "No working video encoder was found, so videos will fail.\n\n"
                                                "Continue with images only?", parent=self):
                return
            s = dataclasses.replace(s, videos=False)

        self._finish_handled = False
        self._clear_jobs()
        self.log("head", f"Session started  ·  {s.input_dir}  →  {s.output_dir}")
        self.session = Session(s, self.hw, self.history_path, self.log_threadsafe)
        self.session_thread = threading.Thread(target=self.session.run, name="prism-session", daemon=True)
        self.session_thread.start()
        keep_awake(True)
        self.taskbar.state(self._hwnd(), TaskbarProgress.INDETERMINATE)
        self._set_running_view()
        self._tick()

    def _toggle_pause(self):
        sess = self.session
        if not sess or sess.finished:
            return
        if sess.paused:
            sess.resume()
            self.pause_btn.configure(text="Pause")
            self.taskbar.state(self._hwnd(), TaskbarProgress.NORMAL)
            self.log("info", "Resumed.")
        else:
            sess.pause()
            self.pause_btn.configure(text="Resume")
            self.taskbar.state(self._hwnd(), TaskbarProgress.PAUSED)
            self.log("info", "Paused. Every encoder process is suspended until you resume.")

    def _stop(self):
        sess = self.session
        if not sess or sess.finished:
            return
        if not messagebox.askyesno("Stop compression?", "Finished files are kept; anything in progress is discarded.",
                                   parent=self):
            return
        sess.cancel()
        self.stop_btn.configure(state="disabled")
        self.pause_btn.configure(state="disabled")
        self.status_title.configure(text="Stopping…")
        self.taskbar.state(self._hwnd(), TaskbarProgress.ERROR)
        self.log("warn", "Stopping: killing encoders and discarding partial files…")

    def _tick(self):
        sess = self.session
        if sess is None:
            return
        snap = sess.snapshot()
        self._render_snapshot(snap)
        if snap["finished"]:
            self._finish_session(snap)
        else:
            self.after(250, self._tick)
        # Under full CPU load the timers above never leave Tk idle, so geometry and redraws (idle
        # tasks) would be starved and labels keep the size of their previous text. Flush them here.
        self.update_idletasks()

    def _render_snapshot(self, snap: dict):
        phase = snap["phase"]
        titles = {"starting": "Starting", "scanning": "Scanning", "indexing": "Fingerprinting", "planning": "Planning",
                  "compressing": "Compressing", "done": "Finishing"}
        if snap["paused"]:
            title, sub = "Paused", "Encoders suspended. Press Resume to continue."
        elif phase in ("scanning", "indexing"):
            title, sub = titles[phase], snap["detail"]
        elif phase == "compressing":
            title = "Compressing"
            sub = f"{snap['done']:,} of {snap['total']:,} files"
        else:
            title, sub = titles.get(phase, phase.title()), ""
        if not (self.session and self.session.ctl.cancelled):
            self.status_title.configure(text=title)
        self.status_sub.configure(text=sub)

        frac = snap["fraction"] if phase == "compressing" else 0.0
        self.percent.configure(text=f"{frac * 100:.0f}%" if phase == "compressing" else "—")
        self.progress.set(frac)
        meta = [f"Elapsed {fmt_duration(snap['elapsed'])}"]
        if snap["eta"] is not None and phase == "compressing":
            meta.append(f"ETA {fmt_duration(snap['eta'])}")
        self.meta.configure(text="  ·  ".join(meta))
        if phase == "compressing" and snap["total"]:
            self.taskbar.state(self._hwnd(), TaskbarProgress.PAUSED if snap["paused"] else TaskbarProgress.NORMAL)
            self.taskbar.value(self._hwnd(), int(frac * 1000), 1000)
        self._render_stats(snap)
        self._render_jobs(snap["active"])

    def _render_stats(self, snap):
        saved = snap["bytes_in"] - snap["bytes_out"]
        ratio = f"−{saved / snap['bytes_in']:.0%} on compressed files" if snap["bytes_in"] else " "
        self.tile_done.set(f"{snap['compressed']:,}", f"of {snap['total']:,} queued" if snap["total"] else " ")
        self.tile_saved.set(fmt_bytes(saved) if saved > 0 else "0 B", ratio, C.GREEN if saved > 0 else C.TEXT)
        self.tile_skip.set(f"{snap['skipped']:,}", "already optimal / done before")
        self.tile_fail.set(f"{snap['failed']:,}", "see Activity for details" if snap["failed"] else " ",
                           C.RED if snap["failed"] else C.TEXT)

    def _clear_jobs(self):
        for row in self._job_rows.values():
            row.destroy()
        self._job_rows.clear()
        self.jobs_empty.grid(row=0, column=0, pady=58)
        self.active_count.configure(text="")

    def _render_jobs(self, active: list):
        keys = []
        for d in active:
            key = (d["kind"], d["name"], d.get("size"))
            keys.append(key)
            if key not in self._job_rows:
                self._job_rows[key] = JobRow(self.jobs_frame, d["kind"])
            self._job_rows[key].update_from(d)
        for key in list(self._job_rows):
            if key not in keys:
                self._job_rows.pop(key).destroy()
        for i, key in enumerate(keys):
            self._job_rows[key].grid(row=i + 1, column=0, sticky="ew", pady=(0, 6), padx=(4, 4))
        if keys:
            self.jobs_empty.grid_forget()
        else:
            self.jobs_empty.grid(row=0, column=0, pady=58)
        self.active_count.configure(text=f"{len(keys)} active" if keys else "")

    def _finish_session(self, snap):
        if self._finish_handled:
            return
        self._finish_handled = True
        sess = self.session
        keep_awake(False)
        hwnd = self._hwnd()
        self.taskbar.state(hwnd, TaskbarProgress.NOPROGRESS)
        self._clear_jobs()
        outcome = sess.outcome
        saved = snap["bytes_in"] - snap["bytes_out"]
        summary = (f"{snap['compressed']:,} compressed  ·  {snap['skipped']:,} skipped  ·  {snap['failed']:,} failed"
                   f"  ·  {fmt_bytes(max(0, saved))} saved  ·  {fmt_duration(snap['elapsed'])}")
        titles = {"completed": "Complete", "cancelled": "Stopped", "empty": "Nothing to do", "error": "Stopped by an error"}
        self.status_title.configure(text=titles.get(outcome, "Finished"))
        self.status_sub.configure(text=summary if outcome != "empty" else "No matching images or videos in the source folder.")
        if outcome == "completed":
            self.percent.configure(text="100%" if snap["total"] else "—")
            self.progress.set(1 if snap["total"] else 0)
        self.log("head" if outcome == "completed" else "warn", f"Session {titles.get(outcome, outcome).lower()}  ·  {summary}")
        self._set_idle_view()        # the finished session stays referenced so its summary remains on screen
        if IS_WIN and hwnd:
            try:
                ctypes.windll.user32.FlashWindow(hwnd, True)
            except Exception:
                pass
        if self.shutdown_var.get() and outcome == "completed":
            self.shutdown_var.set(False)
            ShutdownDialog(self)

    def _hwnd(self):
        if not IS_WIN:
            return None
        try:
            return ctypes.windll.user32.GetParent(self.winfo_id())
        except Exception:
            return None

    # -- history / watch -----------------------------------------------------
    def _clear_history(self):
        if self.batch_running:
            return
        if not messagebox.askyesno("Reset history?", "Prism will forget which files it has already compressed, "
                                                     "so the next run processes everything again.", icon="warning", parent=self):
            return
        h = History(self.history_path)
        ok = h.clear()
        h.close()
        self.log("info" if ok else "error", "History reset." if ok else "Could not reset history (database locked?).")

    def _open_watch(self):
        if not HAS_WATCHDOG:
            messagebox.showerror("Prism", "Watch folder needs the 'watchdog' package (pip install watchdog).", parent=self)
            return
        WatchWindow(self)

    def start_watch(self, watch_dir: str, out_dir: str):
        self.stop_watch()
        self.watch = WatchService(self, watch_dir, out_dir)
        self.watch.start()
        self.log("info", f"Watching {watch_dir}  →  {out_dir}")

    def stop_watch(self):
        if self.watch:
            self.watch.stop()
            self.watch = None
            self.log("info", "Watch folder stopped.")

    # -- shutdown ------------------------------------------------------------
    def _on_close(self):
        if self.batch_running:
            if not messagebox.askyesno("Quit Prism?", "Compression is running. Stop it and quit?", parent=self):
                return
            self.session.cancel()
            if self.session_thread:
                self.session_thread.join(timeout=8)
        self.stop_watch()
        if self._save_after:
            self.after_cancel(self._save_after)
        self._save_config()
        keep_awake(False)
        self._flush_logs()
        if self._log_file:
            self._log_file.close()
        self.destroy()


class WatchWindow(ctk.CTkToplevel):
    def __init__(self, app: PrismApp):
        super().__init__(app, fg_color=C.BG)
        self.app = app
        self.title("Watch folder")
        self.geometry("560x400")
        self.resizable(False, False)
        self.transient(app)
        self.grid_columnconfigure(0, weight=1)
        cfg = app.config_data
        self.watch_var = tk.StringVar(value=cfg.get("watch_path", ""))
        self.out_var = tk.StringVar(value=cfg.get("output_path", ""))
        self.auto_var = tk.BooleanVar(value=bool(cfg.get("autostart", False)))

        sec = Section(self, "Watch folder", "New files are compressed automatically once they finish copying.")
        sec.grid(row=0, column=0, sticky="nsew", padx=18, pady=18)
        sec.add(PathRow(sec.body, "Watch", self.watch_var, lambda: self._pick(self.watch_var)), pady=(6, 0))
        sec.add(PathRow(sec.body, "Destination", self.out_var, lambda: self._pick(self.out_var)), pady=(12, 0))
        sec.add(ToggleRow(sec.body, "Start automatically with Prism", self.auto_var,
                          "Uses the current image and video settings."), pady=(14, 0))
        row = ctk.CTkFrame(sec.body, fg_color="transparent")
        row.grid_columnconfigure(0, weight=1)
        self.state_lbl = ctk.CTkLabel(row, text="", font=F.small, text_color=C.TEXT_3, anchor="w")
        self.state_lbl.grid(row=0, column=0, sticky="w")
        self.toggle_btn = ctk.CTkButton(row, text="", width=120, height=38, corner_radius=12, font=F.button,
                                        fg_color=C.ACCENT, hover_color=C.ACCENT_HOVER, command=self._toggle)
        self.toggle_btn.grid(row=0, column=1)
        sec.add(row, pady=(18, 0))
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._refresh()
        self.after(250, lambda: (style_window(self), self.lift()))

    def _pick(self, var):
        path = filedialog.askdirectory(parent=self, initialdir=var.get() or None)
        if path:
            var.set(os.path.normpath(path))

    def _refresh(self):
        on = self.app.watch is not None and self.app.watch.alive
        self.state_lbl.configure(text="● Watching" if on else "Not running", text_color=C.GREEN if on else C.TEXT_3)
        self.toggle_btn.configure(text="Stop" if on else "Start", fg_color=C.FIELD if on else C.ACCENT,
                                  hover_color=C.BORDER_HI if on else C.ACCENT_HOVER)

    def _toggle(self):
        if self.app.watch is not None and self.app.watch.alive:
            self.app.stop_watch()
        else:
            w, o = self.watch_var.get().strip(), self.out_var.get().strip()
            if not os.path.isdir(w):
                messagebox.showerror("Watch folder", "Choose an existing folder to watch.", parent=self)
                return
            if not o:
                messagebox.showerror("Watch folder", "Choose a destination folder.", parent=self)
                return
            if os.path.normcase(os.path.abspath(w)) == os.path.normcase(os.path.abspath(o)):
                messagebox.showerror("Watch folder", "Watch and destination folders must differ.", parent=self)
                return
            os.makedirs(o, exist_ok=True)
            self._save()
            self.app.start_watch(w, o)
        self._refresh()

    def _save(self):
        self.app.config_data.update({"watch_path": self.watch_var.get().strip(), "output_path": self.out_var.get().strip(),
                                     "autostart": bool(self.auto_var.get())})
        self.app._save_config()

    def _close(self):
        self._save()
        self.destroy()


class ShutdownDialog(ctk.CTkToplevel):
    SECONDS = 60

    def __init__(self, app: PrismApp):
        super().__init__(app, fg_color=C.BG)
        self.app = app
        self.title("Shutting down")
        self.geometry("420x210")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.remaining = self.SECONDS
        card = Card(self)
        card.pack(fill="both", expand=True, padx=16, pady=16)
        ctk.CTkLabel(card, text="Compression finished", font=F.status, text_color=C.TEXT).pack(pady=(20, 2))
        self.label = ctk.CTkLabel(card, text="", font=F.body, text_color=C.TEXT_2)
        self.label.pack()
        ctk.CTkButton(card, text="Cancel shutdown", height=40, corner_radius=12, font=F.button, fg_color=C.ACCENT,
                      hover_color=C.ACCENT_HOVER, command=self._abort).pack(pady=(16, 18))
        cmd = ["shutdown", "/s", "/t", str(self.SECONDS)] if IS_WIN else ["shutdown", "-h", "+1"]
        try:
            ok = subprocess.run(cmd, creationflags=engine.NO_WINDOW, capture_output=True).returncode == 0
        except OSError:
            ok = False
        if ok:
            app.log("warn", f"Shutting down in {self.SECONDS} s (cancel in the dialog).")
        else:
            app.log("error", "Could not schedule a shutdown (on macOS/Linux this needs admin rights).")
        self.protocol("WM_DELETE_WINDOW", self._abort)
        self.after(250, lambda: style_window(self))
        self._countdown()

    def _countdown(self):
        if not self.winfo_exists():
            return
        self.label.configure(text=f"This computer shuts down in {self.remaining} s.")
        self.remaining -= 1
        if self.remaining >= 0:
            self.after(1000, self._countdown)

    def _abort(self):
        cmd = ["shutdown", "/a"] if IS_WIN else ["shutdown", "-c"] if sys.platform.startswith("linux") else ["killall", "shutdown"]
        try:
            subprocess.run(cmd, creationflags=engine.NO_WINDOW, capture_output=True)
        except OSError:
            pass
        self.app.log("info", "Shutdown cancelled.")
        self.destroy()


def run():
    if IS_WIN:
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("visiuun.prism.3")
        except Exception:
            pass
    app = PrismApp()
    app.mainloop()
