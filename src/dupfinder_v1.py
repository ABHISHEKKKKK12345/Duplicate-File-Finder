"""
╔══════════════════════════════════════════════════════════════════════╗
║           DUPLICATE FILE FINDER  —  Enterprise Edition               ║
║           Production-Ready · Feature-Complete  v1.0                  ║
╠══════════════════════════════════════════════════════════════════════╣
║  Author  : Abhishek                                                  ║
║  Version : 1.0.0                                                     ║
║  Engine  : SHA-256 content hashing + size pre-filter                 ║
║  Platform: Windows / macOS / Linux  (Python 3.9+)                    ║
║  Deps    : stdlib only                                               ║
╚══════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Dict, Generator, List, Optional, Set, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING  (file only — zero console spam)
# ─────────────────────────────────────────────────────────────────────────────
LOG_DIR          = Path.home() / ".duplicate_finder"
LOG_RETAIN_DAYS  = 7          
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE         = LOG_DIR / f"dupfinder_{datetime.now():%Y%m%d_%H%M%S}.log"
SETTINGS_FILE    = LOG_DIR / "settings.json"


def _purge_old_logs() -> int:
    """Delete log files older than LOG_RETAIN_DAYS. Returns count removed."""
    cutoff  = datetime.now() - timedelta(days=LOG_RETAIN_DAYS)
    removed = 0
    for p in LOG_DIR.glob("dupfinder_*.log"):
        try:
            if datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
log = logging.getLogger("DupFinder")

# Purge stale logs before anything else runs
_old_removed = _purge_old_logs()
log.info(
    "Application starting — Python %s on %s  (purged %d old log(s))",
    sys.version, platform.system(), _old_removed,
)


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS & THEME
# ─────────────────────────────────────────────────────────────────────────────
APP_TITLE   = "Duplicate File Finder"
APP_VERSION = "1.0.0"
CHUNK_SIZE  = 65_536   # 64 KB read chunks for hashing
MIN_FILE_B  = 1        # skip 0-byte files by default

C: Dict[str, str] = {
    "bg":       "#0F1117",
    "panel":    "#181C27",
    "card":     "#1E2335",
    "border":   "#2A2F45",
    "accent":   "#4F8EF7",
    "accent2":  "#F75F4F",
    "accent3":  "#4FF7A0",
    "warn":     "#F7C44F",
    "text":     "#E8ECF5",
    "text_dim": "#6B7394",
    "text_hi":  "#FFFFFF",
    "sel":      "#2D3A5C",
    "danger":   "#C0392B",
    "safe":     "#27AE60",
    "row_even": "#181C27",
    "row_odd":  "#1E2335",
    "keep_bg":  "#1A2E1A",
}

_SYS       = platform.system()
FONT_MONO  = ("Consolas",          10) if _SYS == "Windows" else \
             ("Menlo",             10) if _SYS == "Darwin"  else \
             ("DejaVu Sans Mono",  10)
FONT_BODY  = ("Segoe UI",          10) if _SYS == "Windows" else \
             ("SF Pro Text",       10) if _SYS == "Darwin"  else \
             ("Ubuntu",            10)
FONT_SMALL = (FONT_BODY[0],  9)
FONT_TITLE = (FONT_BODY[0], 14, "bold")
FONT_HEAD  = (FONT_BODY[0], 11, "bold")

# ── system-path roots computed once at module level ──────────────────
_SYS_ROOTS: frozenset[str] = frozenset(
    {
        "C:\\Windows",
        "C:\\$Recycle.Bin",
        "C:\\System Volume Information",
    }
    if _SYS == "Windows"
    else {"/proc", "/sys", "/dev", "/run", "/boot"}
)


# ─────────────────────────────────────────────────────────────────────────────
# DATA MODELS
# ─────────────────────────────────────────────────────────────────────────────
class ScanState(Enum):
    IDLE      = auto()
    SCANNING  = auto()
    DONE      = auto()
    CANCELLED = auto()
    ERROR     = auto()


# FIX PERF-03: __slots__ reduces memory footprint at scale (100k+ entries)
@dataclass
class FileEntry:
    __slots__ = ("path", "size", "mtime", "hash", "error")

    path:  Path
    size:  int
    mtime: float
    hash:  Optional[str]
    error: Optional[str]

    def __init__(self, path: Path, size: int, mtime: float,
                 hash: Optional[str] = None, error: Optional[str] = None):
        self.path  = path
        self.size  = size
        self.mtime = mtime
        self.hash  = hash
        self.error = error

    @property
    def size_hr(self) -> str:
        return _human_size(self.size)

    @property
    def mtime_dt(self) -> str:
        return datetime.fromtimestamp(self.mtime).strftime("%Y-%m-%d %H:%M")

    @property
    def ext(self) -> str:
        return self.path.suffix.lower() or "(none)"

    @property
    def category(self) -> str:
        return _file_category(self.path)


@dataclass
class DuplicateGroup:
    __slots__ = ("hash", "files")

    hash:  str
    files: List[FileEntry]

    def __init__(self, hash: str, files: Optional[List[FileEntry]] = None):
        self.hash  = hash
        self.files = files if files is not None else []

    @property
    def count(self) -> int:
        return len(self.files)

    @property
    def size_each(self) -> int:
        return self.files[0].size if self.files else 0

    @property
    def wasted_bytes(self) -> int:
        return self.size_each * max(0, self.count - 1)

    @property
    def wasted_hr(self) -> str:
        return _human_size(self.wasted_bytes)


# ─────────────────────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────────────────────
def _human_size(b: int) -> str:
    """Return a human-readable file size string."""
    if b < 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024.0:
            return f"{b:.1f} {unit}"
        b /= 1024.0
    return f"{b:.1f} PB"

_CATEGORY_MAP: Dict[str, str] = {
    # ── Images ──────────────────────────────────────────────────────────────
    ".jpg":  "Image", ".jpeg": "Image", ".png":  "Image", ".gif":  "Image",
    ".bmp":  "Image", ".tiff": "Image", ".tif":  "Image", ".webp": "Image",
    ".heic": "Image", ".heif": "Image", ".ico":  "Image", ".svg":  "Image",
    ".raw":  "Image", ".cr2":  "Image", ".nef":  "Image", ".arw":  "Image",
    # ── Video ────────────────────────────────────────────────────────────────
    ".mp4":  "Video", ".mkv":  "Video", ".avi":  "Video", ".mov":  "Video",
    ".wmv":  "Video", ".flv":  "Video", ".webm": "Video", ".m4v":  "Video",
    ".mpeg": "Video", ".mpg":  "Video", ".3gp":  "Video",
    # ── Audio ────────────────────────────────────────────────────────────────
    ".mp3":  "Audio", ".flac": "Audio", ".wav":  "Audio", ".aac":  "Audio",
    ".ogg":  "Audio", ".m4a":  "Audio", ".wma":  "Audio", ".opus": "Audio",
    # ── Documents ────────────────────────────────────────────────────────────
    ".pdf":  "Document", ".doc":  "Document", ".docx": "Document",
    ".xls":  "Document", ".xlsx": "Document", ".ppt":  "Document",
    ".pptx": "Document", ".odt":  "Document", ".ods":  "Document",
    ".txt":  "Document", ".rtf":  "Document", ".csv":  "Document",
    # ── Archives ─────────────────────────────────────────────────────────────
    ".zip": "Archive", ".rar": "Archive", ".7z":  "Archive",
    ".tar": "Archive", ".gz":  "Archive", ".bz2": "Archive",
    ".xz":  "Archive", ".tgz": "Archive",
    # ── Code ─────────────────────────────────────────────────────────────────
    ".py":   "Code", ".js":   "Code", ".ts":   "Code", ".java": "Code",
    ".cpp":  "Code", ".c":    "Code", ".h":    "Code", ".cs":   "Code",
    ".go":   "Code", ".rs":   "Code", ".rb":   "Code", ".php":  "Code",
    ".html": "Code", ".css":  "Code", ".sql":  "Code", ".sh":   "Code",
    # ── Executables ──────────────────────────────────────────────────────────
    ".exe": "Executable", ".msi": "Executable", ".dmg": "Executable",
    ".deb": "Executable", ".rpm": "Executable", ".app": "Executable",
}


def _file_category(p: Path) -> str:
    return _CATEGORY_MAP.get(p.suffix.lower(), "Other")


def _sha256_file(
    path: Path,
    cancel_event: threading.Event,
    progress_cb: Optional[Callable[[int], None]] = None,
) -> str:
    """Stream-hash a file; cancellable; raises InterruptedError if cancelled."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while not cancel_event.is_set():
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
            if progress_cb:
                progress_cb(len(chunk))
    if cancel_event.is_set():
        raise InterruptedError("Scan cancelled")
    return h.hexdigest()


# ── Trash support (platform-aware, no external deps) ─────────────────────────

def _move_to_trash(path: Path) -> Tuple[bool, str]:
    """Move *path* to the system recycle bin / trash.
    Returns (success: bool, error_message: str)."""
    try:
        if _SYS == "Windows":
            return _trash_windows(path)
        elif _SYS == "Darwin":
            return _trash_macos(path)
        else:
            return _trash_linux(path)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _trash_windows(path: Path) -> Tuple[bool, str]:
    """Use SHFileOperationW to move to Recycle Bin on Windows."""
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd",                  wintypes.HWND),
                ("wFunc",                 wintypes.UINT),
                ("pFrom",                 wintypes.LPCWSTR),
                ("pTo",                   wintypes.LPCWSTR),
                ("fFlags",                ctypes.c_ushort),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings",         ctypes.c_void_p),
                ("lpszProgressTitle",     wintypes.LPCWSTR),
            ]

        FO_DELETE          = 0x0003
        FOF_ALLOWUNDO      = 0x0040
        FOF_NOCONFIRMATION = 0x0010
        FOF_SILENT         = 0x0004

        op          = SHFILEOPSTRUCTW()
        op.hwnd     = 0
        op.wFunc    = FO_DELETE
        op.pFrom    = str(path.resolve()) + "\x00\x00"
        op.pTo      = None
        op.fFlags   = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT

        ret = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if ret == 0:
            return True, ""
        return False, f"SHFileOperation returned {ret}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _trash_macos(path: Path) -> Tuple[bool, str]:
    """Use osascript to move to Trash on macOS."""
    script = f'tell app "Finder" to delete POSIX file "{path.resolve()}"'
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=15,
        )
        if r.returncode == 0:
            return True, ""
        return False, r.stderr.decode(errors="replace").strip()
    except FileNotFoundError:
        return False, "osascript not found"
    except subprocess.TimeoutExpired:
        return False, "osascript timed out"


def _trash_linux(path: Path) -> Tuple[bool, str]:
    """Try gio/kioclient/trash-put; fall back to XDG manual implementation."""
    resolved = str(path.resolve())
    for cmd in (
        ["gio",        "trash", resolved],
        ["kioclient5", "move",  resolved, "trash:/"],
        ["kioclient",  "move",  resolved, "trash:/"],
        ["trash-put",           resolved],
    ):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=15)
            if r.returncode == 0:
                return True, ""
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return _xdg_trash(path)


def _xdg_trash(path: Path) -> Tuple[bool, str]:
    """Manual XDG Trash specification implementation for Linux."""
    try:
        trash_dir = Path.home() / ".local" / "share" / "Trash"
        files_dir = trash_dir / "files"
        info_dir  = trash_dir / "info"
        files_dir.mkdir(parents=True, exist_ok=True)
        info_dir.mkdir(parents=True, exist_ok=True)

        dest    = files_dir / path.name
        counter = 1
        while dest.exists():
            dest = files_dir / f"{path.stem}_{counter}{path.suffix}"
            counter += 1

        info_content = (
            "[Trash Info]\n"
            f"Path={path.resolve()}\n"
            f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n"
        )
        (info_dir / (dest.name + ".trashinfo")).write_text(
            info_content, encoding="utf-8"
        )
        shutil.move(str(path), str(dest))
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _open_path_in_explorer(p: Path) -> None:
    """Open a folder (or file's parent) in the native file manager."""
    try:
        target = str(p)
        if _SYS == "Windows":
            os.startfile(target)
        elif _SYS == "Darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except Exception as exc:
        log.warning("Cannot open path %s: %s", p, exc)


# ─────────────────────────────────────────────────────────────────────────────
# SETTINGS MANAGER
# ─────────────────────────────────────────────────────────────────────────────
class Settings:
    """Persist user preferences to ~/.duplicate_finder/settings.json."""

    _defaults: Dict = {
        "last_folders":    [],
        "min_size_kb":     "0",
        "max_size_mb":     "0",
        "include_ext":     "",
        "exclude_ext":     "",
        "skip_hidden":     True,
        "recursive":       True,
        "skip_system":     True,
        "use_trash":       True,
        "window_geometry": "",
    }

    def __init__(self) -> None:
        self._data: Dict = dict(self._defaults)
        self._load()

    def _load(self) -> None:
        try:
            if SETTINGS_FILE.exists():
                loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                for k, v in loaded.items():
                    if k in self._defaults:
                        self._data[k] = v
        except Exception as exc:
            log.warning("Could not load settings: %s", exc)

    def save(self) -> None:
        try:
            SETTINGS_FILE.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("Could not save settings: %s", exc)

    def get(self, key: str):
        return self._data.get(key, self._defaults.get(key))

    def set(self, key: str, value) -> None:
        self._data[key] = value


# ─────────────────────────────────────────────────────────────────────────────
# TOOLTIP  (FIX PERF-01: position clamped to screen bounds)
# ─────────────────────────────────────────────────────────────────────────────
class Tooltip:
    """Lightweight tooltip that follows the cursor near any widget."""

    def __init__(self, widget: tk.Widget, text: str, delay: int = 600) -> None:
        self._widget = widget
        self._text   = text
        self._delay  = delay
        self._job:   Optional[str]        = None
        self._win:   Optional[tk.Toplevel] = None
        widget.bind("<Enter>",  self._on_enter, add="+")
        widget.bind("<Leave>",  self._on_leave, add="+")
        widget.bind("<Button>", self._on_leave, add="+")

    def _on_enter(self, _event=None) -> None:
        self._cancel()
        self._job = self._widget.after(self._delay, self._show)

    def _on_leave(self, _event=None) -> None:
        self._cancel()
        self._hide()

    def _cancel(self) -> None:
        if self._job:
            self._widget.after_cancel(self._job)
            self._job = None

    def _show(self) -> None:
        if self._win:
            return
        # Candidate position
        x = self._widget.winfo_rootx() + 20
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4

        self._win = tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)

        lbl = tk.Label(
            tw, text=self._text,
            font=FONT_SMALL, fg=C["text_hi"], bg=C["border"],
            padx=8, pady=4, relief="flat", justify="left",
        )
        lbl.pack()
        tw.update_idletasks()   # force geometry calculation

        # FIX PERF-01: clamp so tooltip never goes off-screen
        sw = tw.winfo_screenwidth()
        sh = tw.winfo_screenheight()
        tw_w = tw.winfo_reqwidth()
        tw_h = tw.winfo_reqheight()
        x = min(x, sw - tw_w - 4)
        y = min(y, sh - tw_h - 4)
        x = max(x, 4)
        y = max(y, 4)

        tw.wm_geometry(f"+{x}+{y}")

    def _hide(self) -> None:
        if self._win:
            self._win.destroy()
            self._win = None


# ─────────────────────────────────────────────────────────────────────────────
# SCAN ENGINE  (runs in worker thread)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ScanConfig:
    roots:       List[Path]
    min_size:    int       = MIN_FILE_B
    max_size:    int       = 0                        # 0 = unlimited
    include_ext: Set[str] = field(default_factory=set)  # empty = all
    exclude_ext: Set[str] = field(default_factory=set)
    skip_hidden: bool     = True
    skip_system: bool     = True
    recursive:   bool     = True


class ScanEngine:
    """Thread-safe duplicate scanner.  All public attributes are read-only
    from outside; mutations happen only under _lock or before thread start."""

    def __init__(self, config: ScanConfig, msg_queue: queue.Queue) -> None:
        self._cfg  = config
        self._q    = msg_queue
        self._stop = threading.Event()
        self._lock = threading.Lock()

        # counters — always mutated under _lock
        self._total_files   = 0
        self._scanned_files = 0
        self._total_bytes   = 0
        self._hashed_bytes  = 0
        self._errors        = 0

        self.groups: List[DuplicateGroup] = []

    # ── read-only snapshots ───────────────────────────────────────────────────
    @property
    def total_files(self) -> int:
        with self._lock:
            return self._total_files

    @property
    def scanned_files(self) -> int:
        with self._lock:
            return self._scanned_files

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    @property
    def hashed_bytes(self) -> int:
        with self._lock:
            return self._hashed_bytes

    @property
    def errors(self) -> int:
        with self._lock:
            return self._errors

    # ── public ────────────────────────────────────────────────────────────────
    def cancel(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            self._emit("state",  ScanState.SCANNING)
            self._emit("status", "Phase 1/3 — Enumerating files…")

            entries = list(self._enumerate())

            if self._stop.is_set():
                self._emit("state", ScanState.CANCELLED)
                return

            with self._lock:
                self._total_files = len(entries)
                self._total_bytes = sum(e.size for e in entries)

            # ── Phase 2: size pre-filter ──────────────────────────────────────
            self._emit(
                "status",
                f"Phase 2/3 — Pre-filtering by size ({self._total_files:,} files)…",
            )

            size_map: Dict[int, List[FileEntry]] = defaultdict(list)
            for e in entries:
                size_map[e.size].append(e)

            candidates = [
                e for grp in size_map.values() if len(grp) > 1 for e in grp
            ]

            singles = self._total_files - len(candidates)
            self._emit(
                "status",
                f"Phase 2/3 — {len(candidates):,} candidates "
                f"({singles:,} unique by size, skipped)…",
            )

            # Switches progress bar from indeterminate to determinate mode
            # even when there are 0 candidates (shows 0/0 cleanly).
            self._emit("progress_init", len(candidates))

            if not candidates:
                self._emit("result", [])
                self._emit("state",  ScanState.DONE)
                return

            # ── Phase 3: hashing ──────────────────────────────────────────────
            self._emit("status", "Phase 3/3 — Hashing candidates…")
            hash_map: Dict[str, List[FileEntry]] = defaultdict(list)

            for e in candidates:
                if self._stop.is_set():
                    self._emit("state", ScanState.CANCELLED)
                    return
                try:
                    e.hash = _sha256_file(
                        e.path, self._stop,
                        progress_cb=lambda b: self._add_bytes(b),
                    )
                    hash_map[e.hash].append(e)
                except InterruptedError:
                    self._emit("state", ScanState.CANCELLED)
                    return
                except PermissionError as exc:
                    e.error = f"Permission denied: {exc}"
                    with self._lock:
                        self._errors += 1
                    log.warning("Permission denied: %s", e.path)
                except OSError as exc:
                    e.error = str(exc)
                    with self._lock:
                        self._errors += 1
                    log.warning("OS error on %s: %s", e.path, exc)
                except Exception as exc:  # noqa: BLE001
                    e.error = str(exc)
                    with self._lock:
                        self._errors += 1
                    log.exception("Unexpected error hashing %s", e.path)
                finally:
                    with self._lock:
                        self._scanned_files += 1
                    self._emit("progress_tick", self._scanned_files)

            # Build groups (≥ 2 identical hashes), sorted by wasted space desc
            self.groups = sorted(
                [
                    DuplicateGroup(
                        h,
                        files=sorted(fl, key=lambda x: x.mtime),
                    )
                    for h, fl in hash_map.items()
                    if len(fl) > 1
                ],
                key=lambda g: g.wasted_bytes,
                reverse=True,
            )

            self._emit("status", "Scan complete.")
            self._emit("result", self.groups)
            self._emit("state",  ScanState.DONE)
            log.info(
                "Scan done — %d groups, %d errors",
                len(self.groups), self.errors,
            )

        except Exception:  # noqa: BLE001
            log.exception("Fatal engine error")
            self._emit("error", traceback.format_exc())
            self._emit("state", ScanState.ERROR)

    # ── internals ─────────────────────────────────────────────────────────────
    def _add_bytes(self, n: int) -> None:
        with self._lock:
            self._hashed_bytes += n

    def _emit(self, kind: str, data=None) -> None:
        self._q.put((kind, data))

    def _enumerate(self) -> Generator[FileEntry, None, None]:
        cfg  = self._cfg
        seen: Set[str] = set()

        # _SYS_ROOTS — zero per-file allocation inside the hot loop.

        def _walk(root: Path) -> Generator[FileEntry, None, None]:
            if self._stop.is_set():
                return
            try:
                children = list(root.iterdir()) if root.is_dir() else [root]
            except PermissionError:
                log.warning("No permission to list: %s", root)
                return
            except OSError as exc:
                log.warning("OS error listing %s: %s", root, exc)
                return

            for p in children:
                if self._stop.is_set():
                    return
                try:
                    if cfg.skip_hidden and p.name.startswith("."):
                        continue

                    # ── skip system paths using module-level constant ──────
                    if cfg.skip_system:
                        if any(str(p).startswith(s) for s in _SYS_ROOTS):
                            continue

                    try:
                        lst = p.lstat()
                    except OSError:
                        continue

                    if p.is_symlink():
                        continue   # skip all symlinks to avoid loops / double-count

                    if p.is_dir():
                        if cfg.recursive:
                            yield from _walk(p)
                        continue

                    # Deduplicate hard-linked files via inode key
                    inode_key = f"{lst.st_dev}:{lst.st_ino}"
                    if inode_key in seen:
                        continue
                    seen.add(inode_key)

                    size = lst.st_size
                    if size < cfg.min_size:
                        continue
                    if cfg.max_size and size > cfg.max_size:
                        continue

                    ext = p.suffix.lower()
                    if cfg.include_ext and ext not in cfg.include_ext:
                        continue
                    if cfg.exclude_ext and ext in cfg.exclude_ext:
                        continue

                    yield FileEntry(path=p, size=size, mtime=lst.st_mtime)

                except PermissionError:
                    log.warning("No permission: %s", p)
                except OSError as exc:
                    log.warning("Stat error %s: %s", p, exc)
                except Exception:  # noqa: BLE001
                    log.exception("Unexpected enum error: %s", p)

        for root in cfg.roots:
            yield from _walk(root)


# ─────────────────────────────────────────────────────────────────────────────
# SPLASH SCREEN
# ─────────────────────────────────────────────────────────────────────────────
class SplashScreen(tk.Toplevel):
    _STEPS = [
        "Loading theme engine…",
        "Mounting file scanner…",
        "Initialising hash worker pool…",
        "Building UI components…",
        "Ready.",
    ]

    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master)
        self.overrideredirect(True)
        self.configure(bg=C["bg"])
        W, H = 480, 290
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2}")

        border = tk.Frame(self, bg=C["accent"], padx=2, pady=2)
        border.pack(fill="both", expand=True)
        inner = tk.Frame(border, bg=C["bg"])
        inner.pack(fill="both", expand=True)

        tk.Label(
            inner, text="⬡ DUPFINDER",
            font=(FONT_BODY[0], 28, "bold"),
            fg=C["accent"], bg=C["bg"],
        ).pack(pady=(36, 4))
        tk.Label(
            inner, text="Enterprise Duplicate File Engine",
            font=FONT_BODY, fg=C["text_dim"], bg=C["bg"],
        ).pack()
        tk.Label(
            inner, text=f"v{APP_VERSION}",
            font=FONT_SMALL, fg=C["text_dim"], bg=C["bg"],
        ).pack(pady=(2, 16))

        self._pb = ttk.Progressbar(inner, length=320, mode="determinate")
        self._pb.pack()
        self._lbl = tk.Label(
            inner, text="Initialising…",
            font=FONT_SMALL, fg=C["text_dim"], bg=C["bg"],
        )
        self._lbl.pack(pady=8)
        tk.Label(
            inner,
            text="© 2026  Abhishek",
            font=(FONT_BODY[0], 8), fg=C["border"], bg=C["bg"],
        ).pack(side="bottom", pady=6)

        self._step = 0
        self.after(120, self._advance)

    def _advance(self) -> None:
        if self._step < len(self._STEPS):
            self._lbl.config(text=self._STEPS[self._step])
            self._pb["value"] = (self._step + 1) / len(self._STEPS) * 100
            self._step += 1
            self.after(200, self._advance)
        else:
            self.after(300, self.destroy)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN APPLICATION
# ─────────────────────────────────────────────────────────────────────────────
class DuplicateFinderApp(tk.Tk):

    def __init__(self) -> None:
        super().__init__()
        self.withdraw()   # hide until splash completes

        self._settings  = Settings()
        self._setup_style()
        self._build_window()
        self._init_state()
        self._msg_queue: queue.Queue                 = queue.Queue()
        self._engine:    Optional[ScanEngine]        = None
        self._scan_thread: Optional[threading.Thread] = None

        splash = SplashScreen(self)
        self.after(100, lambda: self._wait_splash(splash))

    # ── bootstrap ─────────────────────────────────────────────────────────────
    def _wait_splash(self, splash: SplashScreen) -> None:
        if splash.winfo_exists():
            self.after(150, lambda: self._wait_splash(splash))
            return

        for folder in self._settings.get("last_folders"):
            p = Path(folder)
            if p.is_dir():
                self._folder_lb.insert("end", str(p))

        self._ent_min_size.set(self._settings.get("min_size_kb"))
        self._ent_max_size.set(self._settings.get("max_size_mb"))
        self._ent_include.set(self._settings.get("include_ext"))
        self._ent_exclude.set(self._settings.get("exclude_ext"))
        self._var_hidden.set(self._settings.get("skip_hidden"))
        self._var_recursive.set(self._settings.get("recursive"))
        self._var_system.set(self._settings.get("skip_system"))
        self._var_use_trash.set(self._settings.get("use_trash"))

        geo = self._settings.get("window_geometry")
        if geo:
            try:
                self.geometry(geo)
            except Exception:
                pass

        self.deiconify()
        self._poll_queue()

    def _init_state(self) -> None:
        self._state = ScanState.IDLE
        self._start_time: Optional[float] = None
        self._sort_reverse: Dict[str, bool] = {}
        self._groups:           List[DuplicateGroup] = []
        self._filtered_groups:  List[DuplicateGroup] = []
        self._selected_group_idx: Optional[int]      = None

    # ── style ─────────────────────────────────────────────────────────────────
    def _setup_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(
            ".", background=C["bg"], foreground=C["text"],
            font=FONT_BODY, fieldbackground=C["card"],
            troughcolor=C["panel"],
            selectbackground=C["sel"], selectforeground=C["text_hi"],
        )

        style.configure("TFrame",       background=C["bg"])
        style.configure("Card.TFrame",  background=C["card"])
        style.configure("Panel.TFrame", background=C["panel"])

        style.configure("TLabel",        background=C["bg"], foreground=C["text"])
        style.configure("Dim.TLabel",    background=C["bg"], foreground=C["text_dim"])
        style.configure("Card.TLabel",   background=C["card"], foreground=C["text"])
        style.configure(
            "Accent.TLabel",
            background=C["bg"], foreground=C["accent"],
            font=(FONT_BODY[0], 10, "bold"),
        )
        style.configure(
            "Danger.TLabel",
            background=C["bg"], foreground=C["accent2"],
            font=(FONT_BODY[0], 10, "bold"),
        )

        style.configure(
            "TButton", background=C["card"], foreground=C["text"],
            borderwidth=0, padding=(10, 6), relief="flat",
        )
        style.map(
            "TButton",
            background=[("active", C["border"]), ("disabled", C["panel"])],
            foreground=[("disabled", C["text_dim"])],
        )

        style.configure(
            "Accent.TButton", background=C["accent"],
            foreground=C["text_hi"], font=(FONT_BODY[0], 10, "bold"),
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#3A7AE4"), ("disabled", C["panel"])],
        )

        style.configure(
            "Danger.TButton", background=C["danger"],
            foreground=C["text_hi"], font=(FONT_BODY[0], 10, "bold"),
        )
        style.map(
            "Danger.TButton",
            background=[("active", "#E74C3C"), ("disabled", C["panel"])],
        )

        style.configure(
            "TProgressbar", background=C["accent"],
            troughcolor=C["panel"], borderwidth=0, thickness=6,
        )

        style.configure(
            "Treeview", background=C["card"], foreground=C["text"],
            fieldbackground=C["card"], borderwidth=0, rowheight=22,
        )
        style.configure(
            "Treeview.Heading", background=C["panel"],
            foreground=C["accent"], font=(FONT_BODY[0], 9, "bold"),
            borderwidth=0,
        )
        style.map(
            "Treeview",
            background=[("selected", C["sel"])],
            foreground=[("selected", C["text_hi"])],
        )

        style.configure("TNotebook", background=C["bg"], borderwidth=0)
        style.configure(
            "TNotebook.Tab", background=C["panel"],
            foreground=C["text_dim"], padding=(14, 6), borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", C["card"])],
            foreground=[("selected", C["text_hi"])],
        )

        style.configure(
            "TEntry", fieldbackground=C["card"],
            foreground=C["text"], insertcolor=C["text"],
            borderwidth=1, relief="flat",
        )
        style.configure("TCheckbutton", background=C["card"], foreground=C["text"])
        style.configure("TCombobox",    fieldbackground=C["card"], foreground=C["text"])
        style.configure("TSeparator",   background=C["border"])

    # ── window ────────────────────────────────────────────────────────────────
    def _build_window(self) -> None:
        self.title(f"⬡ {APP_TITLE}  v{APP_VERSION}")
        self.configure(bg=C["bg"])
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        W, H   = min(1320, sw - 80), min(840, sh - 80)
        self.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2}")
        self.minsize(960, 640)

        self._build_header()
        self._build_body()
        self._build_statusbar()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._bind_shortcuts()

    def _bind_shortcuts(self) -> None:
        self.bind_all("<F5>",        lambda _: self._start_scan())
        self.bind_all("<Escape>",    lambda _: self._cancel_scan())
        self.bind_all("<Control-q>", lambda _: self._on_close())
        self.bind_all("<Control-e>", lambda _: self._export_report())
        self._file_tree.bind("<Control-a>", lambda _: self._select_all_dupes())
        self._file_tree.bind("<Control-d>", lambda _: self._deselect_all())

    # ── header ────────────────────────────────────────────────────────────────
    def _build_header(self) -> None:
        hdr = tk.Frame(self, bg=C["panel"], height=54)
        hdr.pack(fill="x", side="top")
        hdr.pack_propagate(False)

        tk.Label(
            hdr, text="⬡", font=(FONT_BODY[0], 22, "bold"),
            fg=C["accent"], bg=C["panel"],
        ).pack(side="left", padx=(16, 4))
        tk.Label(
            hdr, text="DUPFINDER", font=(FONT_BODY[0], 14, "bold"),
            fg=C["text_hi"], bg=C["panel"],
        ).pack(side="left", padx=(0, 8))
        tk.Label(
            hdr, text=f"Enterprise Edition  v{APP_VERSION}",
            font=FONT_SMALL, fg=C["text_dim"], bg=C["panel"],
        ).pack(side="left")

        self._hdr_groups = self._hdr_kpi(hdr, "Groups",      "—")
        self._hdr_files  = self._hdr_kpi(hdr, "Duplicates",  "—")
        self._hdr_saved  = self._hdr_kpi(hdr, "Reclaimable", "—")
        self._hdr_time   = self._hdr_kpi(hdr, "Scan Time",   "—")

    def _hdr_kpi(self, parent: tk.Widget, label: str, value: str) -> tk.Label:
        frm = tk.Frame(parent, bg=C["border"], padx=1, pady=1)
        frm.pack(side="right", padx=6, pady=8)
        inner = tk.Frame(frm, bg=C["panel"], width=120, height=55)
        inner.pack()
        inner.pack_propagate(False)
        tk.Label(
            inner, text=label, font=FONT_SMALL,
            fg=C["text_dim"], bg=C["panel"],
        ).pack()
        lbl = tk.Label(
            inner, text=value, font=(FONT_BODY[0], 11, "bold"),
            fg=C["accent"], bg=C["panel"],
        )
        lbl.pack()
        return lbl

    # ── body (notebook) ───────────────────────────────────────────────────────
    def _build_body(self) -> None:
        self._nb = ttk.Notebook(self)
        self._nb.pack(fill="both", expand=True, padx=8, pady=(6, 0))

        self._tab_scan    = ttk.Frame(self._nb, style="Panel.TFrame")
        self._tab_results = ttk.Frame(self._nb, style="Panel.TFrame")
        self._tab_log     = ttk.Frame(self._nb, style="Panel.TFrame")

        self._nb.add(self._tab_scan,    text="  ⊕  Scan Setup  ")
        self._nb.add(self._tab_results, text="  ☰  Results     ")
        self._nb.add(self._tab_log,     text="  ✦  Activity Log")

        self._build_scan_tab()
        self._build_results_tab()
        self._build_log_tab()

    # ── scan tab ──────────────────────────────────────────────────────────────
    def _build_scan_tab(self) -> None:
        p = self._tab_scan
        p.columnconfigure(0, weight=1)

        # ── TARGET FOLDERS ───────────────────────────────────────────────────
        sec1 = self._section(p, "TARGET FOLDERS", row=0)
        sec1.columnconfigure(0, weight=1)
        sec1.rowconfigure(0, weight=1)

        lf = tk.Frame(sec1, bg=C["card"])
        lf.grid(row=0, column=0, sticky="nsew", padx=12, pady=(6, 0))
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)

        self._folder_lb = tk.Listbox(
            lf, bg=C["card"], fg=C["text"], font=FONT_MONO,
            selectbackground=C["sel"], selectforeground=C["text_hi"],
            relief="flat", borderwidth=0, height=5, activestyle="none",
        )
        self._folder_lb.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self._folder_lb.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self._folder_lb.configure(yscrollcommand=sb.set)
        self._folder_lb.bind("<Delete>",    lambda _: self._remove_folder())
        self._folder_lb.bind("<BackSpace>", lambda _: self._remove_folder())

        btn_row = tk.Frame(sec1, bg=C["panel"])
        btn_row.grid(row=1, column=0, sticky="w", padx=12, pady=6)

        b_add = ttk.Button(
            btn_row, text="＋ Add Folder",
            command=self._add_folder, style="Accent.TButton",
        )
        b_add.pack(side="left", padx=(0, 6))
        Tooltip(b_add, "Browse for a folder to include in the scan")

        b_rem = ttk.Button(btn_row, text="－ Remove",
                           command=self._remove_folder)
        b_rem.pack(side="left", padx=(0, 6))
        Tooltip(b_rem, "Remove the selected folder (or press Delete)")

        b_clr = ttk.Button(btn_row, text="✕ Clear All",
                           command=self._clear_folders)
        b_clr.pack(side="left")
        Tooltip(b_clr, "Remove all folders from the list")

        # ── FILTERS & OPTIONS ────────────────────────────────────────────────
        sec2 = self._section(p, "FILTERS & OPTIONS", row=1)
        for c in range(4):
            sec2.columnconfigure(c, weight=1)

        self._ent_min_size = tk.StringVar(value="0")
        self._ent_max_size = tk.StringVar(value="0")
        self._ent_include  = tk.StringVar(value="")
        self._ent_exclude  = tk.StringVar(value="")

        self._build_labeled_entry(sec2, "Min File Size (KB)",           self._ent_min_size, 0, 0)
        self._build_labeled_entry(sec2, "Max File Size (MB)  [0=∞]",    self._ent_max_size, 0, 1)
        self._build_labeled_entry(sec2, "Include Extensions (.jpg …)",  self._ent_include,  1, 0)
        self._build_labeled_entry(sec2, "Exclude Extensions (.tmp …)",  self._ent_exclude,  1, 1)

        cb_frame = tk.Frame(sec2, bg=C["panel"])
        cb_frame.grid(row=2, column=0, columnspan=4, sticky="w", padx=12, pady=4)
        self._var_hidden    = self._checkbox(cb_frame, "Skip hidden files",    True)
        self._var_recursive = self._checkbox(cb_frame, "Recursive subfolders", True)
        self._var_system    = self._checkbox(cb_frame, "Skip system paths",    True)
        self._var_use_trash = self._checkbox(cb_frame, "Move to Trash (safer)", True)
        Tooltip(
            cb_frame.winfo_children()[-1],
            "When enabled, deletes move files to the system trash instead\n"
            "of permanent deletion. Uncheck for irreversible removal.",
        )

        # ── SCAN PROGRESS ────────────────────────────────────────────────────
        sec3 = self._section(p, "SCAN PROGRESS", row=2)
        sec3.columnconfigure(0, weight=1)

        self._progress_lbl = tk.Label(
            sec3,
            text="Ready to scan.  (F5 = Start  ·  Esc = Cancel)",
            font=FONT_BODY, fg=C["text_dim"], bg=C["panel"],
        )
        self._progress_lbl.grid(row=0, column=0, sticky="w", padx=12, pady=(6, 2))

        self._progressbar = ttk.Progressbar(sec3, mode="determinate", length=100)
        self._progressbar.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 4))

        self._prog_detail = tk.Label(
            sec3, text="", font=FONT_SMALL, fg=C["text_dim"], bg=C["panel"],
        )
        self._prog_detail.grid(row=2, column=0, sticky="w", padx=12, pady=(0, 8))

        # ── ACTION ROW ───────────────────────────────────────────────────────
        act = tk.Frame(p, bg=C["bg"])
        act.grid(row=3, column=0, sticky="ew", padx=8, pady=8)

        self._btn_scan = ttk.Button(
            act, text="▶  Start Scan  (F5)",
            command=self._start_scan, style="Accent.TButton", width=18,
        )
        self._btn_scan.pack(side="left", padx=(0, 8))

        self._btn_cancel = ttk.Button(
            act, text="■  Cancel  (Esc)",
            command=self._cancel_scan, state="disabled",
        )
        self._btn_cancel.pack(side="left", padx=(0, 8))

        self._btn_export = ttk.Button(
            act, text="↧  Export Report  (Ctrl+E)",
            command=self._export_report, state="disabled",
        )
        self._btn_export.pack(side="left")

    # ── results tab ───────────────────────────────────────────────────────────
    def _build_results_tab(self) -> None:
        p = self._tab_results
        p.columnconfigure(0, weight=1)
        p.rowconfigure(1, weight=1)

        tb = tk.Frame(p, bg=C["panel"])
        tb.grid(row=0, column=0, sticky="ew", padx=8, pady=6)

        tk.Label(tb, text="Filter:", font=FONT_SMALL,
                 fg=C["text_dim"], bg=C["panel"]).pack(side="left", padx=(0, 4))
        self._filter_var = tk.StringVar()
        self._filter_var.trace_add("write", lambda *_: self._apply_filter())
        fe = ttk.Entry(tb, textvariable=self._filter_var, width=28)
        fe.pack(side="left", padx=(0, 10))
        Tooltip(fe, "Filter groups by file path substring")

        tk.Label(tb, text="Category:", font=FONT_SMALL,
                 fg=C["text_dim"], bg=C["panel"]).pack(side="left", padx=(0, 4))
        self._cat_var = tk.StringVar(value="All")
        cats = ["All", "Image", "Video", "Audio", "Document",
                "Archive", "Code", "Executable", "Other"]
        self._cat_combo = ttk.Combobox(
            tb, textvariable=self._cat_var,
            values=cats, state="readonly", width=12,
        )
        self._cat_combo.pack(side="left", padx=(0, 8))
        self._cat_combo.bind("<<ComboboxSelected>>", lambda _: self._apply_filter())

        ttk.Button(tb, text="⟳ Reset Filter",
                   command=self._reset_filter).pack(side="left", padx=(0, 20))

        b_gd = ttk.Button(
            tb, text="⚠  Delete All Visible Dupes",
            command=self._delete_all_visible, style="Danger.TButton",
        )
        b_gd.pack(side="right", padx=4)
        Tooltip(b_gd, "Delete non-kept files in ALL currently visible groups")

        b_sa = ttk.Button(tb, text="☑ Select All Dupes",
                          command=self._select_all_dupes)
        b_sa.pack(side="right", padx=4)
        Tooltip(b_sa, "Select all duplicate (non-kept) rows in this group (Ctrl+A)")

        b_da = ttk.Button(tb, text="☐ Deselect All",
                          command=self._deselect_all)
        b_da.pack(side="right", padx=4)
        Tooltip(b_da, "Deselect all rows (Ctrl+D)")

        b_ao = ttk.Button(tb, text="⇄ Auto-Keep Oldest",
                          command=self._auto_keep_oldest)
        b_ao.pack(side="right", padx=4)
        Tooltip(
            b_ao,
            "Mark the oldest file in each group as Keep;\n"
            "review each group before deleting",
        )

        pw = ttk.PanedWindow(p, orient="horizontal")
        pw.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 4))

        # ── left pane: group list ─────────────────────────────────────────────
        left = tk.Frame(pw, bg=C["card"])
        pw.add(left, weight=35)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)

        tk.Label(left, text="DUPLICATE GROUPS", font=FONT_SMALL,
                 fg=C["text_dim"], bg=C["card"],
                 ).grid(row=0, column=0, columnspan=2, sticky="w",
                        padx=8, pady=(6, 2))

        self._group_tree = ttk.Treeview(
            left, columns=("size", "count", "waste"),
            show="headings", selectmode="browse",
        )
        for col, hd, w in [
            ("size",  "File Size",   90),
            ("count", "Copies",      60),
            ("waste", "Reclaimable", 105),
        ]:
            self._group_tree.heading(col, text=hd,
                                     command=lambda c=col: self._sort_groups(c))
            self._group_tree.column(col, width=w, anchor="center")
        self._group_tree.grid(row=1, column=0, sticky="nsew")
        gsb = ttk.Scrollbar(left, orient="vertical",
                            command=self._group_tree.yview)
        gsb.grid(row=1, column=1, sticky="ns")
        self._group_tree.configure(yscrollcommand=gsb.set)
        self._group_tree.bind("<<TreeviewSelect>>", self._on_group_select)
        self._group_tree.bind("<Button-3>", self._on_group_right_click)

        # ── right pane: file list ─────────────────────────────────────────────
        right = tk.Frame(pw, bg=C["card"])
        pw.add(right, weight=65)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        tk.Label(right, text="FILES IN GROUP", font=FONT_SMALL,
                 fg=C["text_dim"], bg=C["card"],
                 ).grid(row=0, column=0, columnspan=2, sticky="w",
                        padx=8, pady=(6, 2))

        cols = ("keep", "path", "size", "modified", "category")
        self._file_tree = ttk.Treeview(
            right, columns=cols, show="headings", selectmode="extended",
        )
        for col, hd, w in [
            ("keep",     "⚐ Keep?",  62),
            ("path",     "Path",    390),
            ("size",     "Size",     90),
            ("modified", "Modified", 135),
            ("category", "Type",      90),
        ]:
            self._file_tree.heading(col, text=hd)
            self._file_tree.column(col, width=w,
                                   anchor="center" if w < 120 else "w")
        self._file_tree.grid(row=1, column=0, sticky="nsew")
        fsb = ttk.Scrollbar(right, orient="vertical",
                            command=self._file_tree.yview)
        fsb.grid(row=1, column=1, sticky="ns")
        self._file_tree.configure(yscrollcommand=fsb.set)
        self._file_tree.bind("<Double-1>", self._on_file_double_click)
        self._file_tree.bind("<Button-3>", self._on_file_right_click)
        self._file_tree.bind("<Delete>",   lambda _: self._delete_selected())

        fab = tk.Frame(right, bg=C["card"])
        fab.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=6)

        b_ol = ttk.Button(fab, text="🗁  Open Location",
                          command=self._open_location)
        b_ol.pack(side="left", padx=(0, 6))
        Tooltip(b_ol, "Open the containing folder in the file manager\n"
                "(also double-click a row)")

        b_tk = ttk.Button(fab, text="✔ Toggle Keep",
                          command=self._toggle_keep)
        b_tk.pack(side="left", padx=(0, 20))
        Tooltip(b_tk, "Make the selected file the 'kept' copy;\n"
                "all others become candidates for deletion")

        b_del = ttk.Button(
            fab, text="⚠  Delete Selected",
            style="Danger.TButton", command=self._delete_selected,
        )
        b_del.pack(side="right", padx=(6, 0))
        Tooltip(b_del, "Delete selected duplicate files\n"
                "(uses Trash if 'Move to Trash' is checked)  (Del)")

        b_mv = ttk.Button(fab, text="↷  Move to Folder…",
                          command=self._move_selected)
        b_mv.pack(side="right", padx=(0, 6))
        Tooltip(b_mv, "Move selected duplicate files to a chosen folder")

    # ── log tab ───────────────────────────────────────────────────────────────
    def _build_log_tab(self) -> None:
        p = self._tab_log
        p.columnconfigure(0, weight=1)
        p.rowconfigure(0, weight=1)

        self._log_text = tk.Text(
            p, bg=C["bg"], fg=C["text_dim"], font=FONT_MONO,
            relief="flat", borderwidth=0, state="disabled", wrap="none",
        )
        self._log_text.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(p, orient="vertical",  command=self._log_text.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(p, orient="horizontal", command=self._log_text.xview)
        hsb.grid(row=1, column=0, sticky="ew")
        self._log_text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self._log_text.tag_configure("info",  foreground=C["text_dim"])
        self._log_text.tag_configure("ok",    foreground=C["accent3"])
        self._log_text.tag_configure("warn",  foreground=C["warn"])
        self._log_text.tag_configure("error", foreground=C["accent2"])
        self._log_text.tag_configure(
            "head", foreground=C["accent"],
            font=(FONT_MONO[0], 10, "bold"),
        )

        tb2 = tk.Frame(p, bg=C["bg"])
        tb2.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=4)
        ttk.Button(tb2, text="Clear Log",
                   command=self._clear_log).pack(side="left")
        ttk.Button(
            tb2, text="Open Log File",
            command=lambda: _open_path_in_explorer(LOG_FILE.parent),
        ).pack(side="left", padx=8)

    # ── status bar ────────────────────────────────────────────────────────────
    def _build_statusbar(self) -> None:
        bar = tk.Frame(self, bg=C["panel"], height=26)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        self._status_lbl = tk.Label(
            bar, text="Idle  ·  No scan active",
            font=FONT_SMALL, fg=C["text_dim"], bg=C["panel"], anchor="w",
        )
        self._status_lbl.pack(side="left", padx=10, fill="y")

        self._clock_lbl = tk.Label(
            bar, text="", font=FONT_SMALL, fg=C["text_dim"], bg=C["panel"],
        )
        self._clock_lbl.pack(side="right", padx=10)
        self._tick_clock()

        tk.Label(
            bar, text=f"Log → {LOG_FILE}",
            font=FONT_SMALL, fg=C["border"], bg=C["panel"],
        ).pack(side="right", padx=20)

    def _tick_clock(self) -> None:
        self._clock_lbl.config(text=datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
        self.after(1000, self._tick_clock)

    # FIX PERF-02: live elapsed-time KPI updated every second during a scan
    def _tick_elapsed(self) -> None:
        """Update the 'Scan Time' KPI every second while scan is active."""
        if self._state != ScanState.SCANNING or self._start_time is None:
            return
        elapsed = time.monotonic() - self._start_time
        self._hdr_time.config(text=f"{elapsed:.1f}s")
        self.after(1000, self._tick_elapsed)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _section(self, parent: tk.Widget, title: str, row: int) -> tk.Frame:
        """Create a labelled section panel and return its inner frame."""
        outer = tk.Frame(parent, bg=C["bg"])
        outer.grid(row=row, column=0, sticky="nsew", padx=8, pady=(8, 0))
        outer.columnconfigure(0, weight=1)
        tk.Label(
            outer, text=f"  {title}",
            font=(FONT_BODY[0], 9, "bold"), fg=C["accent"], bg=C["border"],
        ).grid(row=0, column=0, sticky="ew")
        inner = tk.Frame(outer, bg=C["panel"])
        inner.grid(row=1, column=0, sticky="nsew")
        inner.columnconfigure(0, weight=1)
        return inner

    @staticmethod
    def _build_labeled_entry(
        parent: tk.Widget, label: str, var: tk.StringVar, row: int, col: int,
    ) -> None:
        frm = tk.Frame(parent, bg=C["panel"])
        frm.grid(row=row, column=col, sticky="w", padx=12, pady=4)
        tk.Label(frm, text=label, font=FONT_SMALL,
                 fg=C["text_dim"], bg=C["panel"]).pack(anchor="w")
        ttk.Entry(frm, textvariable=var, width=22).pack(anchor="w")

    @staticmethod
    def _checkbox(parent: tk.Widget, label: str, default: bool) -> tk.BooleanVar:
        var = tk.BooleanVar(value=default)
        ttk.Checkbutton(parent, text=label, variable=var,
                        style="TCheckbutton").pack(side="left", padx=(0, 14))
        return var

    def _log(self, msg: str, level: str = "info") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_text.configure(state="normal")
        self._log_text.insert("end", f"[{ts}] {msg}\n", level)
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

    def _set_status(self, msg: str) -> None:
        self._status_lbl.config(text=msg)

    # ── folder management ─────────────────────────────────────────────────────
    def _get_folder_list(self) -> List[Path]:
        return [Path(self._folder_lb.get(i))
                for i in range(self._folder_lb.size())]

    def _add_folder(self) -> None:
        path = filedialog.askdirectory(title="Select folder to scan")
        if not path:
            return
        p        = Path(path)
        existing = [self._folder_lb.get(i)
                    for i in range(self._folder_lb.size())]
        if str(p) in existing:
            self._log(f"Already in list: {p}", "warn")
            return
        self._folder_lb.insert("end", str(p))
        self._log(f"Added: {p}", "ok")

    def _remove_folder(self) -> None:
        sel = self._folder_lb.curselection()
        if sel:
            self._folder_lb.delete(sel[0])

    def _clear_folders(self) -> None:
        self._folder_lb.delete(0, "end")

    # ── scan lifecycle ────────────────────────────────────────────────────────
    def _build_config(self) -> Optional[ScanConfig]:
        roots = self._get_folder_list()
        if not roots:
            messagebox.showwarning("No Folder",
                                   "Please add at least one folder to scan.")
            return None

        bad = [r for r in roots if not r.is_dir()]
        if bad:
            messagebox.showerror(
                "Missing Folder",
                "These folders no longer exist:\n" +
                "\n".join(str(b) for b in bad),
            )
            return None

        def _parse_size(val: str, megabytes: bool = False) -> int:
            try:
                v = float(val.strip().replace(",", ""))
                if v < 0:
                    return 0
                return int(v * (1_048_576 if megabytes else 1_024))
            except (ValueError, OverflowError):
                return 0

        def _parse_exts(s: str) -> Set[str]:
            result: Set[str] = set()
            for part in re.split(r"[,\s]+", s.strip()):
                if not part:
                    continue
                ext = part.lower() if part.startswith(".") else f".{part.lower()}"
                result.add(ext)
            return result

        min_b = _parse_size(self._ent_min_size.get())
        max_b = _parse_size(self._ent_max_size.get(), megabytes=True)
        inc   = _parse_exts(self._ent_include.get())
        exc   = _parse_exts(self._ent_exclude.get())

        return ScanConfig(
            roots       = roots,
            min_size    = max(1, min_b),
            max_size    = max_b,
            include_ext = inc,
            exclude_ext = exc,
            skip_hidden = self._var_hidden.get(),
            recursive   = self._var_recursive.get(),
            skip_system = self._var_system.get(),
        )

    def _start_scan(self) -> None:
        if self._state == ScanState.SCANNING:
            return

        cfg = self._build_config()
        if cfg is None:
            return

        # Reset results state
        self._groups.clear()
        self._filtered_groups.clear()
        self._selected_group_idx = None
        self._group_tree.delete(*self._group_tree.get_children())
        self._file_tree.delete(*self._file_tree.get_children())
        self._reset_kpis()

        # Drain leftover messages from any previous scan
        while not self._msg_queue.empty():
            try:
                self._msg_queue.get_nowait()
            except queue.Empty:
                break

        self._engine      = ScanEngine(cfg, self._msg_queue)
        self._scan_thread = threading.Thread(
            target=self._engine.run, daemon=True, name="ScanWorker",
        )
        self._start_time = time.monotonic()
        self._state      = ScanState.SCANNING

        self._btn_scan.config(state="disabled")
        self._btn_cancel.config(state="normal")
        self._btn_export.config(state="disabled")
        self._progressbar.configure(mode="indeterminate")
        self._progressbar.start(12)

        self._log("━" * 64, "head")
        self._log(f"Scan started — {len(cfg.roots)} root(s)", "head")
        for r in cfg.roots:
            self._log(f"  Root: {r}", "info")
        if cfg.include_ext:
            self._log(f"  Include: {', '.join(sorted(cfg.include_ext))}", "info")
        if cfg.exclude_ext:
            self._log(f"  Exclude: {', '.join(sorted(cfg.exclude_ext))}", "info")

        self._scan_thread.start()
        # FIX PERF-02: start live elapsed-time ticker
        self._tick_elapsed()
        self._nb.select(0)

    def _cancel_scan(self) -> None:
        if self._engine and self._state == ScanState.SCANNING:
            self._engine.cancel()
            self._log("Cancellation requested…", "warn")

    # ── queue pump (main thread, every 100 ms) ────────────────────────────────
    def _poll_queue(self) -> None:
        try:
            while True:
                kind, data = self._msg_queue.get_nowait()
                self._handle_msg(kind, data)
        except queue.Empty:
            pass
        finally:
            self.after(100, self._poll_queue)

    def _handle_msg(self, kind: str, data) -> None:
        if kind == "status":
            self._progress_lbl.config(text=data)
            self._set_status(data)
            self._log(data, "info")

        elif kind == "progress_init":
            self._progressbar.stop()
            self._progressbar.configure(
                mode="determinate",
                maximum=max(1, data),
                value=0,
            )
            if data:
                self._log(f"Hashing {data:,} candidate files…", "info")
            else:
                self._log("No size-matching candidates found.", "info")

        elif kind == "progress_tick":
            self._progressbar.configure(value=data)
            eng     = self._engine
            elapsed = time.monotonic() - (self._start_time or time.monotonic())
            if eng:
                total    = eng.total_files or 1
                hb       = eng.hashed_bytes
                mb       = hb / 1_048_576
                speed    = mb / max(elapsed, 0.01)
                pct      = data / total * 100
                remaining = (total - data) / max(data / max(elapsed, 0.01), 1)
                eta_str   = (
                    f"{remaining:.0f}s remaining"
                    if remaining < 3600
                    else "calculating…"
                )
                self._prog_detail.config(
                    text=(
                        f"{data:,} / {total:,} files  ·  "
                        f"{_human_size(hb)} hashed  ·  "
                        f"{speed:.1f} MB/s  ·  {pct:.1f}%  ·  {eta_str}"
                    )
                )

        elif kind == "result":
            self._on_scan_result(data)

        elif kind == "state":
            self._on_state_change(data)

        elif kind == "error":
            self._log(f"FATAL:\n{data}", "error")
            messagebox.showerror(
                "Scan Error",
                f"An unexpected error occurred:\n\n{data[:800]}",
            )

    def _on_state_change(self, state: ScanState) -> None:
        self._state = state
        self._progressbar.stop()
        self._btn_scan.config(state="normal")
        self._btn_cancel.config(state="disabled")

        elapsed = time.monotonic() - (self._start_time or time.monotonic())
        self._hdr_time.config(text=f"{elapsed:.1f}s")

        if state == ScanState.DONE:
            self._progressbar.configure(mode="determinate", maximum=1, value=1)
            self._log(f"✔ Scan complete in {elapsed:.2f}s", "ok")
            eng = self._engine
            if eng and eng.errors:
                self._log(
                    f"⚠  {eng.errors} file(s) could not be read "
                    "(permission denied or I/O error — see log file)",
                    "warn",
                )
            self._btn_export.config(state="normal")
            self._nb.select(1)

        elif state == ScanState.CANCELLED:
            self._log("Scan cancelled.", "warn")
            self._set_status("Scan cancelled.")
            self._progressbar.configure(mode="determinate", maximum=1, value=0)

        elif state == ScanState.ERROR:
            self._set_status("Scan failed — check Activity Log.")

    def _on_scan_result(self, groups: List[DuplicateGroup]) -> None:
        self._groups          = groups
        self._filtered_groups = groups.copy()
        self._selected_group_idx = None
        self._populate_group_tree(groups)
        self._update_kpis()

        total_dupes  = sum(g.count - 1 for g in groups)
        total_wasted = sum(g.wasted_bytes for g in groups)

        status = (
            f"{len(groups):,} duplicate groups  ·  "
            f"{total_dupes:,} redundant files  ·  "
            f"{_human_size(total_wasted)} reclaimable"
        )
        self._set_status(status)
        self._log(
            f"Found {len(groups):,} groups, "
            f"{total_dupes:,} duplicates, "
            f"{_human_size(total_wasted)} reclaimable",
            "ok",
        )

        self._nb.tab(1, text=f"  ☰  Results  ({len(groups):,})  ")

        if groups:
            self._show_scan_summary(groups, total_dupes, total_wasted)
        else:
            messagebox.showinfo("Scan Complete",
                                "No duplicate files were found.")

    # ── group tree ────────────────────────────────────────────────────────────
    def _populate_group_tree(self, groups: List[DuplicateGroup]) -> None:
        self._group_tree.delete(*self._group_tree.get_children())
        for i, g in enumerate(groups):
            tag = "even" if i % 2 == 0 else "odd"
            self._group_tree.insert(
                "", "end", iid=str(i),
                values=(_human_size(g.size_each), g.count, g.wasted_hr),
                tags=(tag,),
            )
        self._group_tree.tag_configure("even", background=C["row_even"])
        self._group_tree.tag_configure("odd",  background=C["row_odd"])

    def _on_group_select(self, _event=None) -> None:
        sel = self._group_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx < len(self._filtered_groups):
            self._selected_group_idx = idx
            self._populate_file_tree(self._filtered_groups[idx])

    def _populate_file_tree(self, grp: DuplicateGroup) -> None:
        self._file_tree.delete(*self._file_tree.get_children())
        for i, fe in enumerate(grp.files):
            keep_lbl = "✔ Keep" if i == 0 else "  —"
            self._file_tree.insert(
                "", "end", iid=str(i),
                values=(keep_lbl, str(fe.path),
                        fe.size_hr, fe.mtime_dt, fe.category),
                tags=("keep" if i == 0 else "dupe",),
            )
        self._file_tree.tag_configure(
            "keep", background=C["keep_bg"], foreground=C["accent3"],
        )
        self._file_tree.tag_configure("dupe", background=C["card"])

    # ── filter & sort ─────────────────────────────────────────────────────────
    def _apply_filter(self) -> None:
        term = self._filter_var.get().lower()
        cat  = self._cat_var.get()

        self._filtered_groups = [
            g for g in self._groups
            if (
                (not term or any(term in str(f.path).lower() for f in g.files))
                and
                (cat == "All" or any(f.category == cat for f in g.files))
            )
        ]

        # a group that's been filtered out, which would show the wrong files.
        self._selected_group_idx = None
        self._file_tree.delete(*self._file_tree.get_children())
        self._populate_group_tree(self._filtered_groups)

    def _reset_filter(self) -> None:
        self._filter_var.set("")
        self._cat_var.set("All")
        self._apply_filter()

    def _sort_groups(self, col: str) -> None:
        rev = not self._sort_reverse.get(col, False)
        self._sort_reverse[col] = rev
        key_fn: Dict[str, Callable] = {
            "size":  lambda g: g.size_each,
            "count": lambda g: g.count,
            "waste": lambda g: g.wasted_bytes,
        }
        self._groups.sort(
            key=key_fn.get(col, lambda g: g.wasted_bytes), reverse=rev,
        )
        self._apply_filter()

    # ── KPI helpers ───────────────────────────────────────────────────────────
    def _update_kpis(self) -> None:
        groups       = self._groups
        total_dupes  = sum(g.count - 1 for g in groups)
        total_wasted = sum(g.wasted_bytes for g in groups)
        self._hdr_groups.config(text=f"{len(groups):,}")
        self._hdr_files.config(text=f"{total_dupes:,}")
        self._hdr_saved.config(text=_human_size(total_wasted))

    def _reset_kpis(self) -> None:
        for lbl in (self._hdr_groups, self._hdr_files, self._hdr_saved):
            lbl.config(text="—")
        self._prog_detail.config(text="")
        self._progressbar.configure(mode="determinate", maximum=1, value=0)

    # ── selection helpers ─────────────────────────────────────────────────────
    def _select_all_dupes(self) -> None:
        """Select all non-kept rows in the current file tree. (Ctrl+A)"""
        dupes = [iid for iid in self._file_tree.get_children() if iid != "0"]
        if dupes:
            self._file_tree.selection_set(*dupes)

    def _deselect_all(self) -> None:
        self._file_tree.selection_remove(*self._file_tree.get_children())

    def _auto_keep_oldest(self) -> None:
        """Sort each group so the oldest file (by mtime) is first (= Keep)."""
        if not self._groups:
            return
        for g in self._groups:
            g.files.sort(key=lambda f: f.mtime)
        if (
            self._selected_group_idx is not None
            and self._selected_group_idx < len(self._filtered_groups)
        ):
            self._populate_file_tree(
                self._filtered_groups[self._selected_group_idx]
            )
        messagebox.showinfo(
            "Auto-Keep Complete",
            "Oldest file is now marked as Keep in every group.\n\n"
            "Select a group on the left, verify the list, then\n"
            "use 'Select All Dupes' → 'Delete Selected' to clean up.",
        )

    def _toggle_keep(self) -> None:
        """Promote the selected file to the kept (index-0) position."""
        if self._selected_group_idx is None:
            return
        sel = self._file_tree.selection()
        if not sel:
            messagebox.showinfo("Select File",
                                "Click a duplicate row to mark it as Keep.")
            return
        idx = int(sel[0])
        if idx == 0:
            return

        grp = self._filtered_groups[self._selected_group_idx]
        if idx >= len(grp.files):
            return

        promoted = grp.files.pop(idx)
        grp.files.insert(0, promoted)
        self._populate_file_tree(grp)
        self._file_tree.focus("0")
        self._file_tree.selection_set("0")

    def _on_file_double_click(self, _event=None) -> None:
        self._open_location()

    def _open_location(self) -> None:
        sel = self._file_tree.selection()
        if not sel or self._selected_group_idx is None:
            return
        idx = int(sel[0])
        grp = self._filtered_groups[self._selected_group_idx]
        if idx < len(grp.files):
            _open_path_in_explorer(grp.files[idx].path.parent)

    # ── context menus ─────────────────────────────────────────────────────────
    def _on_group_right_click(self, event) -> None:
        iid = self._group_tree.identify_row(event.y)
        if not iid:
            return
        self._group_tree.selection_set(iid)
        menu = tk.Menu(
            self, tearoff=0, bg=C["card"], fg=C["text"],
            activebackground=C["sel"], activeforeground=C["text_hi"],
            bd=0, relief="flat",
        )
        menu.add_command(label="Select All Dupes in Group",
                         command=self._select_all_dupes)
        menu.add_command(label="Auto-Keep Oldest",
                         command=self._auto_keep_oldest)
        menu.add_separator()
        menu.add_command(label="Delete All Visible Dupes",
                         command=self._delete_all_visible)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _on_file_right_click(self, event) -> None:
        iid = self._file_tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self._file_tree.selection():
            self._file_tree.selection_set(iid)
        menu = tk.Menu(
            self, tearoff=0, bg=C["card"], fg=C["text"],
            activebackground=C["sel"], activeforeground=C["text_hi"],
            bd=0, relief="flat",
        )
        menu.add_command(label="Open File Location", command=self._open_location)
        menu.add_command(label="Toggle Keep",        command=self._toggle_keep)
        menu.add_separator()
        menu.add_command(label="Delete Selected",  command=self._delete_selected)
        menu.add_command(label="Move to Folder…",  command=self._move_selected)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ── file action helpers ───────────────────────────────────────────────────
    def _get_selected_dupe_entries(
        self,
    ) -> List[Tuple[DuplicateGroup, int, FileEntry]]:
        """Return (group, file_index, FileEntry) for each selected non-keep row."""
        if self._selected_group_idx is None:
            return []
        grp    = self._filtered_groups[self._selected_group_idx]
        result = []
        for iid in self._file_tree.selection():
            idx = int(iid)
            if idx == 0:
                continue   # always protect the kept file
            if idx < len(grp.files):
                result.append((grp, idx, grp.files[idx]))
        return result

    def _do_delete(self, fe: FileEntry) -> Tuple[bool, str]:
        """Delete a single FileEntry via Trash or permanent unlink.
        Returns (success, error_message)."""
        if not fe.path.exists():
            return False, "File not found (already gone?)"
        if self._var_use_trash.get():
            ok, err = _move_to_trash(fe.path)
            if ok:
                log.info("Trashed: %s", fe.path)
                return True, ""
            # Trash failed — fall back to permanent delete
            log.warning("Trash failed (%s), falling back to permanent: %s",
                        err, fe.path)
            try:
                fe.path.unlink()
                log.info("Permanently deleted (after trash failure): %s", fe.path)
                return True, ""
            except OSError as exc:
                return False, str(exc)
        else:
            try:
                fe.path.unlink()
                log.info("Deleted: %s", fe.path)
                return True, ""
            except PermissionError as exc:
                return False, f"Permission denied: {exc}"
            except OSError as exc:
                return False, str(exc)

    def _delete_selected(self) -> None:
        items = self._get_selected_dupe_entries()
        if not items:
            messagebox.showinfo(
                "Nothing Selected",
                "Select one or more duplicate (non-kept) files to delete.\n"
                "The ✔ Keep file is always protected.",
            )
            return

        trash_mode = self._var_use_trash.get()
        total_size = sum(fe.size for _, _, fe in items)
        verb       = "to Trash" if trash_mode else "PERMANENTLY"

        if not messagebox.askyesno(
            "Confirm Delete",
            f"Move {len(items)} file(s) {verb}?\n\n"
            f"Space freed: {_human_size(total_size)}\n"
            + ("" if trash_mode else "\n⚠  This action CANNOT be undone."),
            icon="warning",
        ):
            return

        ok = fail = 0
        for _, _, fe in items:
            success, err = self._do_delete(fe)
            if success:
                ok   += 1
                self._log(
                    f"{'Trashed' if trash_mode else 'Deleted'}: {fe.path}", "warn",
                )
            else:
                fail += 1
                self._log(f"Failed: {fe.path} — {err}", "error")

        msg = f"{'Trashed' if trash_mode else 'Deleted'} {ok} file(s)."
        if fail:
            msg += f"\n{fail} file(s) could not be removed (see Activity Log)."
        messagebox.showinfo("Delete Complete", msg)
        self._log(f"Delete complete: {ok} ok, {fail} failed", "ok")
        self._refresh_after_delete()

    def _delete_all_visible(self) -> None:
        """Delete all non-kept files across every currently visible group."""
        if not self._filtered_groups:
            messagebox.showinfo("Nothing to Delete",
                                "Run a scan first or clear the filter.")
            return

        all_dupes = [
            (grp, i, fe)
            for grp in self._filtered_groups
            for i, fe in enumerate(grp.files)
            if i != 0
        ]
        if not all_dupes:
            messagebox.showinfo("Nothing to Delete",
                                "No duplicate files to remove.")
            return

        trash_mode = self._var_use_trash.get()
        total_size = sum(fe.size for _, _, fe in all_dupes)
        verb       = "to Trash" if trash_mode else "PERMANENTLY"

        if not messagebox.askyesno(
            "Confirm Bulk Delete",
            f"This will move {len(all_dupes)} duplicate files {verb} across "
            f"{len(self._filtered_groups)} groups.\n\n"
            f"Space freed: ≈ {_human_size(total_size)}\n"
            + (
                ""
                if trash_mode
                else "\n⚠  PERMANENT. This action CANNOT be undone.\n"
                     "   Enable 'Move to Trash' for a safer option."
            ),
            icon="warning",
        ):
            return

        ok = fail = 0
        for _, _, fe in all_dupes:
            success, err = self._do_delete(fe)
            if success:
                ok   += 1
                self._log(
                    f"{'Trashed' if trash_mode else 'Deleted'}: {fe.path}", "warn",
                )
            else:
                fail += 1
                self._log(f"Failed: {fe.path} — {err}", "error")

        messagebox.showinfo(
            "Bulk Delete Complete",
            f"{'Trashed' if trash_mode else 'Deleted'} {ok} file(s).\n"
            f"{fail} failed.",
        )
        self._log(f"Bulk delete: {ok} ok, {fail} failed", "ok")
        self._refresh_after_delete()

    def _refresh_after_delete(self) -> None:
        """Prune dissolved groups and repopulate both trees."""
        # Keep only groups where ≥ 2 files still exist on disk
        for g in self._groups:
            g.files = [f for f in g.files if f.path.exists()]
        self._groups = [g for g in self._groups if len(g.files) >= 2]

        # and clears the file pane, then re-populates correctly.
        self._apply_filter()
        self._update_kpis()

    def _move_selected(self) -> None:
        items = self._get_selected_dupe_entries()
        if not items:
            messagebox.showinfo("Nothing Selected",
                                "Select duplicate (non-kept) files to move.")
            return
        dest = filedialog.askdirectory(title="Move duplicates to…")
        if not dest:
            return
        dest_path = Path(dest)
        dest_path.mkdir(parents=True, exist_ok=True)

        ok = fail = rename = 0
        for _, _, fe in items:
            if not fe.path.exists():
                fail += 1
                self._log(f"Not found: {fe.path}", "warn")
                continue
            target = dest_path / fe.path.name
            if target.exists():
                stem   = target.stem
                suffix = target.suffix
                target = dest_path / f"{stem}_{int(time.time_ns())}{suffix}"
                rename += 1
            try:
                shutil.move(str(fe.path), str(target))
                ok += 1
                self._log(f"Moved: {fe.path} → {target}", "info")
            except Exception as exc:  # noqa: BLE001
                fail += 1
                self._log(f"Move failed: {fe.path} — {exc}", "error")

        messagebox.showinfo(
            "Move Complete",
            f"Moved {ok} file(s) to {dest}.\n"
            f"{fail} failed.  {rename} renamed (name collision).",
        )
        self._log(f"Move complete: {ok} ok, {fail} failed, {rename} renamed", "ok")
        self._refresh_after_delete()

    # ── export ────────────────────────────────────────────────────────────────
    def _export_report(self) -> None:
        if self._state == ScanState.SCANNING:
            messagebox.showinfo("Scan Running",
                                "Please wait for the scan to finish.")
            return
        if not self._groups:
            messagebox.showinfo("Nothing to Export", "Run a scan first.")
            return

        path = filedialog.asksaveasfilename(
            title="Save Report",
            defaultextension=".csv",
            filetypes=[
                ("CSV Report", "*.csv"),
                ("JSON Data",  "*.json"),
                ("All Files",  "*.*"),
            ],
        )
        if not path:
            return

        try:
            p = Path(path)
            if p.suffix.lower() == ".json":
                self._export_json(p)
            else:
                self._export_csv(p)
            messagebox.showinfo("Export Complete", f"Report saved:\n{p}")
            self._log(f"Report exported: {p}", "ok")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Export Error", str(exc))
            log.exception("Export error")

    def _export_csv(self, p: Path) -> None:
        # Group_Reclaimable_Bytes is now populated for EVERY row
        # in the group (not just the keep row), making the CSV unambiguous.
        rows = []
        for i, g in enumerate(self._groups, 1):
            for j, fe in enumerate(g.files):
                rows.append({
                    "Group":                    i,
                    "Status":                   "KEEP" if j == 0 else "DUPLICATE",
                    "Path":                     str(fe.path),
                    "Size_Bytes":               fe.size,
                    "Size_HR":                  fe.size_hr,
                    "Modified":                 fe.mtime_dt,
                    "Category":                 fe.category,
                    "Extension":                fe.ext,
                    "SHA256":                   fe.hash or "",
                    "Group_Reclaimable_Bytes":  g.wasted_bytes,   # all rows
                    "Group_Reclaimable_HR":     g.wasted_hr,      # all rows
                })
        if not rows:
            return
        with p.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    def _export_json(self, p: Path) -> None:
        out = {
            "scan_date":   datetime.now().isoformat(),
            "app_version": APP_VERSION,
            "summary": {
                "groups":            len(self._groups),
                "total_duplicates":  sum(g.count - 1 for g in self._groups),
                "reclaimable_bytes": sum(g.wasted_bytes for g in self._groups),
                "reclaimable_hr":    _human_size(
                    sum(g.wasted_bytes for g in self._groups)
                ),
            },
            "groups": [
                {
                    "hash":         g.hash,
                    "count":        g.count,
                    "size_each":    g.size_each,
                    "size_each_hr": _human_size(g.size_each),
                    "wasted_bytes": g.wasted_bytes,
                    "wasted_hr":    g.wasted_hr,
                    "files": [
                        {
                            "path":     str(f.path),
                            "size":     f.size,
                            "modified": f.mtime_dt,
                            "category": f.category,
                            "keep":     (i == 0),
                        }
                        for i, f in enumerate(g.files)
                    ],
                }
                for g in self._groups
            ],
        }
        with p.open("w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)

    # ── post-scan summary popup ───────────────────────────────────────────────
    def _show_scan_summary(
        self,
        groups: List[DuplicateGroup],
        total_dupes: int,
        total_wasted: int,
    ) -> None:
        dlg = tk.Toplevel(self)
        dlg.title("Scan Summary")
        dlg.configure(bg=C["bg"])
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        W, H = 440, 360
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        dlg.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2}")

        tk.Label(dlg, text="✔  Scan Complete", font=FONT_TITLE,
                 fg=C["accent3"], bg=C["bg"]).pack(pady=(20, 6))
        tk.Label(dlg, text="Here's what was found:", font=FONT_SMALL,
                 fg=C["text_dim"], bg=C["bg"]).pack()

        card = tk.Frame(dlg, bg=C["card"], padx=20, pady=16)
        card.pack(fill="x", padx=24, pady=12)

        rows_data = [
            ("Duplicate Groups",  f"{len(groups):,}",               C["accent"]),
            ("Redundant Files",   f"{total_dupes:,}",               C["accent2"]),
            ("Reclaimable Space", _human_size(total_wasted),        C["accent3"]),
            ("Largest Group",     f"{max(g.count for g in groups):,} copies",
                                                                     C["warn"]),
            ("Biggest Waste",     groups[0].wasted_hr,              C["accent2"]),
        ]
        for label, value, colour in rows_data:
            row = tk.Frame(card, bg=C["card"])
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, font=FONT_BODY, fg=C["text_dim"],
                     bg=C["card"], width=22, anchor="w").pack(side="left")
            tk.Label(row, text=value, font=(FONT_BODY[0], 10, "bold"),
                     fg=colour, bg=C["card"]).pack(side="left")

        eng = self._engine
        if eng and eng.errors:
            tk.Label(
                dlg,
                text=f"⚠  {eng.errors} file(s) could not be read",
                font=FONT_SMALL, fg=C["warn"], bg=C["bg"],
            ).pack()

        elapsed = time.monotonic() - (self._start_time or time.monotonic())
        tk.Label(dlg, text=f"Scan time: {elapsed:.2f}s",
                 font=FONT_SMALL, fg=C["text_dim"], bg=C["bg"]).pack(pady=(0, 6))

        ttk.Button(
            dlg, text="View Results →",
            style="Accent.TButton",
            command=lambda: [dlg.destroy(), self._nb.select(1)],
        ).pack(pady=(8, 16))

    # ── settings persistence ──────────────────────────────────────────────────
    def _persist_settings(self) -> None:
        self._settings.set(
            "last_folders",
            [self._folder_lb.get(i) for i in range(self._folder_lb.size())],
        )
        self._settings.set("min_size_kb",      self._ent_min_size.get())
        self._settings.set("max_size_mb",      self._ent_max_size.get())
        self._settings.set("include_ext",      self._ent_include.get())
        self._settings.set("exclude_ext",      self._ent_exclude.get())
        self._settings.set("skip_hidden",      self._var_hidden.get())
        self._settings.set("recursive",        self._var_recursive.get())
        self._settings.set("skip_system",      self._var_system.get())
        self._settings.set("use_trash",        self._var_use_trash.get())
        self._settings.set("window_geometry",  self.geometry())
        self._settings.save()

    # ── close ─────────────────────────────────────────────────────────────────
    def _on_close(self) -> None:
        if self._state == ScanState.SCANNING:
            if not messagebox.askyesno(
                "Scan in Progress",
                "A scan is running. Cancel it and exit?",
                icon="warning",
            ):
                return
            if self._engine:
                self._engine.cancel()
            if self._scan_thread and self._scan_thread.is_alive():
                self._scan_thread.join(timeout=5)

        self._persist_settings()
        log.info("Application closing.")
        for handler in logging.getLogger().handlers:
            try:
                handler.flush()
                handler.close()
            except Exception:
                pass
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    try:
        app = DuplicateFinderApp()
        app.mainloop()
    except Exception:  # noqa: BLE001
        log.exception("Unhandled top-level exception")
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Fatal Error",
                f"An unexpected error occurred.\n\n"
                f"Details written to:\n{LOG_FILE}",
            )
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
