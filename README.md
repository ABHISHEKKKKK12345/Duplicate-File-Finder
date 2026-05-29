<div align="center">

<img src="https://img.shields.io/badge/⬡-DUPFINDER-4F8EF7?style=for-the-badge&labelColor=0F1117&color=4F8EF7" alt="DupFinder" height="40"/>

# Duplicate File Finder — Enterprise Edition

> **A production-ready, cross-platform desktop application that finds and eliminates duplicate files using SHA-256 content hashing — built entirely on Python's standard library.**

<br/>

[![Python](https://img.shields.io/badge/Python-3.9%2B-4F8EF7?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-4FF7A0?style=flat-square)](https://github.com/)
[![License](https://img.shields.io/badge/License-MIT-F7C44F?style=flat-square)](./LICENSE)
[![Dependencies](https://img.shields.io/badge/Dependencies-stdlib%20only-F75F4F?style=flat-square)](./requirements.txt)
[![GUI](https://img.shields.io/badge/GUI-Tkinter-4F8EF7?style=flat-square)](https://docs.python.org/3/library/tkinter.html)

<br/>

![DupFinder Banner](https://via.placeholder.com/900x280/0F1117/4F8EF7?text=⬡+DUPFINDER+Enterprise+%E2%80%94+SHA-256+Content+Hashing+Engine)

</div>

---

## Table of Contents

- [Overview](#-overview)
- [Key Features](#-key-features)
- [Screenshots](#-screenshots)
- [How It Works](#-how-it-works)
- [Installation](#-installation)
- [Building the Executable](#-building-the-executable)
- [Usage Guide](#-usage-guide)
- [Configuration & Filters](#-configuration--filters)
- [Export & Reporting](#-export--reporting)
- [Project Structure](#-project-structure)
- [Technical Architecture](#-technical-architecture)
- [Platform Notes](#-platform-notes)
- [Logging & Diagnostics](#-logging--diagnostics)
- [Known Limitations](#-known-limitations)
- [Author](#-author)
- [License](#-license)

---

## 🔍 Overview

**Duplicate File Finder** is a feature-complete desktop application that scans one or more directories, identifies files with identical content (regardless of filename), and gives you full control to review, keep, delete, or move the duplicates — safely and efficiently.

Unlike filename-based tools, this application uses **SHA-256 cryptographic hashing** to guarantee byte-for-byte accuracy. It will never incorrectly flag two different files as duplicates, even if they share a name.

Built with a two-phase pipeline — size pre-filter → SHA-256 hash — the engine avoids hashing unique-sized files entirely, making it dramatically faster than naive full-hash approaches on large directories.

---

## ✨ Key Features

### 🔒 Accurate Duplicate Detection
- **SHA-256 content hashing** — byte-perfect accuracy, zero false positives
- **Size pre-filter** — files with unique sizes are skipped before hashing (major speed boost)
- **Hard-link deduplication** — inode-based tracking prevents double-counting hard-linked files
- **Symlink-safe** — symbolic links are skipped to avoid infinite loops and double-counting

### 🖥️ Modern Dark-Theme GUI
- Fully themed dark UI built with `tkinter` + `ttk`
- Split-pane results view: Group List (left) ↔ File List (right)
- Sortable columns, inline Keep/Dupe indicators, row-alternating colors
- Live progress bar with MB/s throughput, ETA, and percentage display
- Header KPI dashboard: Groups · Duplicates · Reclaimable Space · Scan Time
- Splash screen on launch, tabbed interface (Scan Setup / Results / Activity Log)

### ⚡ Performance-Optimized Engine
- Runs in a **background worker thread** — UI stays fully responsive during scans
- `__slots__`-based data models reduce memory footprint for 100 000+ file scans
- Queue-based IPC between scan thread and main thread (polled every 100 ms)
- Three-phase progress reporting: Enumerate → Pre-filter → Hash

### 🗑️ Safe File Operations
- **Move to Trash** (default) — uses native system trash on all platforms
  - Windows: `SHFileOperationW` (Recycle Bin)
  - macOS: `osascript` via Finder
  - Linux: `gio trash` / `kioclient` / `trash-put` / XDG manual fallback
- **Permanent delete** — opt-in, requires explicit confirmation
- **Move to Folder** — relocate duplicates to a chosen directory with collision handling

### 🔎 Filtering & Sorting
- Filter results by file path substring
- Filter by category: Image · Video · Audio · Document · Archive · Code · Executable · Other
- Sort groups by File Size, Copy Count, or Reclaimable Space
- Min/Max file size filters (KB / MB)
- Include or Exclude specific extensions
- Skip hidden files, system paths, or disable recursion

### 📋 Export & Reporting
- **CSV export** — one row per file, with Group ID, Status (KEEP/DUPLICATE), Path, Size, Modified date, Category, Extension, SHA-256 hash, and Reclaimable bytes
- **JSON export** — structured hierarchical format with full scan summary and per-group file arrays

### ♻️ Settings Persistence
- Remembers last-used folders, size filters, extension filters, and window geometry across sessions
- Settings stored in `~/.duplicate_finder/settings.json`
- Auto-purges log files older than 7 days

---

## 📸 Screenshots

> *The application running on a dark desktop (Windows 11 shown; identical on macOS and Linux)*

| Scan Setup Tab | Results View |
|---|---|
| ![Scan Setup](https://via.placeholder.com/460x280/0F1117/4F8EF7?text=Scan+Setup+%E2%80%94+Folder+Picker+%2B+Filters) | ![Results](https://via.placeholder.com/460x280/1E2335/4FF7A0?text=Results+%E2%80%94+Group+%2B+File+Split+Pane) |

| Activity Log | Scan Summary Dialog |
|---|---|
| ![Log](https://via.placeholder.com/460x280/0F1117/F7C44F?text=Activity+Log+%E2%80%94+Real-time+Output) | ![Summary](https://via.placeholder.com/460x280/1E2335/F75F4F?text=Scan+Summary+%E2%80%94+KPI+Popup) |

---

## ⚙️ How It Works

The scan runs in three sequential phases, all on a dedicated background thread:

```
Phase 1 — Enumerate
  Walk selected directories recursively (or flat)
  Apply: hidden-file filter · system-path filter · size filters · extension filters
  Deduplicate hard-linked files via (dev, inode) pairs
        ↓
Phase 2 — Size Pre-filter
  Group all collected FileEntry objects by size
  Files whose size is unique → cannot have a duplicate → skipped entirely
  Only files sharing a size with ≥ 1 other file advance to Phase 3
        ↓
Phase 3 — SHA-256 Hashing
  Stream each candidate file in 64 KB chunks
  Compute SHA-256 digest
  Group files by identical digest
  Groups with ≥ 2 files → DuplicateGroup
  Sort groups by wasted bytes (descending)
        ↓
Results
  Populate group tree and file tree in the Results tab
  Emit KPI summary to header dashboard
  Show post-scan summary dialog
```

---

## 🚀 Installation

### Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.9 or higher |
| tkinter | Bundled with standard Python (see note below) |
| OS | Windows 10+, macOS 11+, Ubuntu 20.04+ (or equivalent Linux) |

> **Linux note:** `tkinter` is not always included in the system Python. Install it with:
> ```bash
> sudo apt install python3-tk       # Debian / Ubuntu
> sudo dnf install python3-tkinter  # Fedora / RHEL
> sudo pacman -S tk                 # Arch Linux
> ```

### Run from Source

```bash
# 1. Clone the repository
git clone https://github.com/your-username/duplicate-file-finder.git
cd duplicate-file-finder

# 2. (Optional but recommended) Create a virtual environment
python -m venv .venv

# Activate — Windows
.venv\Scripts\activate

# Activate — macOS / Linux
source .venv/bin/activate

# 3. Install dependencies (stdlib only — this is essentially a no-op)
pip install -r requirements.txt

# 4. Launch the application
python src/dupfinder_v1.py
```

---

## 📦 Building the Executable

You can compile DupFinder into a single standalone `.exe` (Windows), `.app` (macOS), or binary (Linux) with **no Python installation required** on the target machine.

### Using PyInstaller (recommended)

```bash
# Install PyInstaller
pip install pyinstaller

# Build a single-file executable
pyinstaller \
  --onefile \
  --windowed \
  --name "DupFinder" \
  --icon assets/icon.ico \
  src/dupfinder_v1.py
```

> **Flag reference:**
> | Flag | Purpose |
> |---|---|
> | `--onefile` | Bundle everything into one executable file |
> | `--windowed` | Suppress the console window (GUI app) |
> | `--name` | Output executable name |
> | `--icon` | Application icon (`.ico` on Windows, `.icns` on macOS) |

The compiled output will appear in the `dist/` folder.

### Windows — additional options

```bash
# Include a version info file and UPX compression
pyinstaller \
  --onefile \
  --windowed \
  --name "DupFinder" \
  --icon assets/icon.ico \
  --version-file assets/version_info.txt \
  --upx-dir /path/to/upx \
  src/dupfinder_v1.py
```

### macOS — create a .app bundle

```bash
pyinstaller \
  --onedir \
  --windowed \
  --name "DupFinder" \
  --icon assets/icon.icns \
  src/dupfinder_v1.py

# The .app bundle is at dist/DupFinder.app
```

### Linux — AppImage (optional, portable)

After building with PyInstaller `--onefile`, the resulting binary is already portable across most Linux distributions that share the same glibc version. For wider compatibility, wrap it in an AppImage using `appimagetool`.

### Alternative: cx_Freeze

```bash
pip install cx_Freeze
# Then use a setup.py — see cx_Freeze documentation for details
```

---

## 📖 Usage Guide

### Step 1 — Add Folders

Click **＋ Add Folder** or drag paths into the folder list. Add as many root directories as needed. The scanner will traverse them all in a single pass.

### Step 2 — Configure Filters *(optional)*

| Setting | Description |
|---|---|
| Min File Size (KB) | Ignore files smaller than this threshold |
| Max File Size (MB) | Ignore files larger than this (0 = no limit) |
| Include Extensions | Only scan these extensions (e.g. `.jpg .png .mp4`) |
| Exclude Extensions | Skip these extensions (e.g. `.tmp .log`) |
| Skip hidden files | Ignore files/folders starting with `.` |
| Recursive subfolders | Scan all nested subdirectories (uncheck for flat scan) |
| Skip system paths | Avoid `C:\Windows`, `/proc`, `/sys`, etc. |
| Move to Trash | Use system trash instead of permanent delete |

### Step 3 — Start the Scan

Press **▶ Start Scan** or hit **F5**. The progress bar and live stats (files/s, MB/s, ETA) update in real time. Press **■ Cancel** or **Esc** to abort cleanly.

### Step 4 — Review Results

After the scan, the **Results** tab opens automatically. On the left, duplicate groups are listed sorted by reclaimable space. Click any group to see all its files on the right.

The **oldest file** in each group is pre-marked as `✔ Keep` (by modification date). You can:
- Click **✔ Toggle Keep** to promote a different file
- Click **⇄ Auto-Keep Oldest** to set the oldest as Keep across all groups
- Use **☑ Select All Dupes** (Ctrl+A) to select all non-kept files in a group
- Right-click any row for a context menu

### Step 5 — Delete or Move

- **⚠ Delete Selected** — removes selected duplicates (to Trash or permanently)
- **↷ Move to Folder…** — relocates selected files to a directory you choose
- **⚠ Delete All Visible Dupes** — bulk-delete across all currently visible groups

All destructive actions show a confirmation dialog before proceeding.

---

## 🔧 Configuration & Filters

### Extension format

Extensions can be entered with or without the leading dot, separated by spaces or commas:

```
.jpg .png .gif
jpg, png, gif       ← also valid
```

### Size filters

- **Min size** is in **kilobytes** (e.g. `100` = ignore files under 100 KB)
- **Max size** is in **megabytes** (e.g. `500` = ignore files over 500 MB; `0` = no limit)

### Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| `F5` | Start scan |
| `Esc` | Cancel scan |
| `Ctrl+E` | Export report |
| `Ctrl+Q` | Quit application |
| `Ctrl+A` | Select all duplicates in current group |
| `Ctrl+D` | Deselect all |
| `Delete` | Delete selected files |
| `Double-click` | Open file location in file manager |

---

## 📊 Export & Reporting

### CSV Format

Each file in a duplicate group occupies one row:

| Column | Description |
|---|---|
| `Group` | Sequential group number |
| `Status` | `KEEP` or `DUPLICATE` |
| `Path` | Absolute file path |
| `Size_Bytes` | File size in bytes |
| `Size_HR` | Human-readable size (e.g. `4.2 MB`) |
| `Modified` | Last-modified timestamp |
| `Category` | Image / Video / Audio / Document / Archive / Code / Executable / Other |
| `Extension` | File extension |
| `SHA256` | Full SHA-256 digest |
| `Group_Reclaimable_Bytes` | Bytes that can be freed from this group |
| `Group_Reclaimable_HR` | Human-readable reclaimable size |

### JSON Format

```json
{
  "scan_date": "2026-05-29T14:30:00",
  "summary": {
    "groups": 42,
    "total_duplicates": 137,
    "reclaimable_bytes": 4831838208,
    "reclaimable_hr": "4.5 GB"
  },
  "groups": [
    {
      "hash": "e3b0c44298fc1c149...",
      "count": 3,
      "size_each": 2097152,
      "wasted_bytes": 4194304,
      "files": [
        { "path": "/home/user/docs/file.pdf", "keep": true },
        { "path": "/home/user/backup/file.pdf", "keep": false }
      ]
    }
  ]
}
```

---

## 🗂️ Project Structure

```
duplicate-file-finder/
│
├── src/
│   └── dupfinder_v1.py              ← Entire application (single-file, self-contained)
│
├── assets/                      ← Optional: icons and version info for packaging
│   ├── icon.ico                 ← Windows executable icon
│   └── icon.icns                ← macOS app bundle icon
│
├── dist/                        ← PyInstaller output (generated, git-ignored)
├── build/                       ← PyInstaller build cache (generated, git-ignored)
│
├── .gitignore                   ← Git ignore rules
├── requirements.txt             ← Dependency manifest (stdlib only)
├── LICENSE                      ← MIT License
└── README.md                    ← This file
```

> All runtime files are created in the user's home directory under `~/.duplicate_finder/` — the project directory itself is never written to during normal operation.

---

## 🏗️ Technical Architecture

```
DuplicateFinderApp (tk.Tk — main thread)
│
├── Settings           — JSON-backed preference persistence
├── SplashScreen       — Startup animation (tk.Toplevel)
├── Tooltip            — Screen-clamped hover tooltips
│
├── ScanEngine         — Background worker (threading.Thread)
│   ├── ScanConfig     — Immutable scan parameters
│   ├── FileEntry      — __slots__ dataclass per file
│   └── DuplicateGroup — Grouped results with wasted-space metrics
│
├── queue.Queue        — Thread-safe IPC channel (100 ms poll)
│
└── UI Components
    ├── Header KPI bar     — Groups · Dupes · Reclaimable · Time
    ├── Tab: Scan Setup    — Folder list, filters, progress
    ├── Tab: Results       — PanedWindow: group tree + file tree
    └── Tab: Activity Log  — Timestamped event stream
```

**Thread safety model:** The `ScanEngine` only writes to its internal counters under `_lock`, and communicates all state changes to the UI exclusively via `queue.Queue`. The main thread polls the queue every 100 ms with `after()`. No tkinter calls are ever made from the worker thread.

---

## 🖥️ Platform Notes

### Windows
- Trash uses `SHFileOperationW` via `ctypes` — no shell32 COM registration needed
- System paths skipped: `C:\Windows`, `C:\$Recycle.Bin`, `C:\System Volume Information`
- Fonts: Consolas (mono), Segoe UI (body)

### macOS
- Trash uses `osascript` calling Finder's `delete` verb
- System paths skipped: `/proc /sys /dev /run /boot` (same as Linux)
- Fonts: Menlo (mono), SF Pro Text (body)

### Linux
- Trash attempts (in order): `gio trash` → `kioclient5` → `kioclient` → `trash-put` → XDG manual
- XDG manual implementation writes `.trashinfo` metadata to `~/.local/share/Trash/info/`
- Fonts: DejaVu Sans Mono (mono), Ubuntu (body)
- **Requires `python3-tk`** — not always bundled with system Python (see Installation)

---

## 📝 Logging & Diagnostics

Log files are written to `~/.duplicate_finder/` on all platforms. Each session creates a new timestamped log:

```
~/.duplicate_finder/
├── dupfinder_20260529_143022.log    ← current session
├── dupfinder_20260528_091145.log    ← previous session
└── settings.json                    ← persisted preferences
```

Logs older than **7 days** are automatically purged at startup. The log path for the current session is displayed in the status bar at the bottom of the application window.

To open the log folder directly from the app: **Activity Log tab → Open Log File**.

---

## ⚠️ Known Limitations

| Limitation | Notes |
|---|---|
| No cloud storage support | Scans local filesystem paths only |
| No network share scanning | UNC paths may work on Windows but are untested |
| Single-threaded hashing | Phase 3 hashes files sequentially; future version may parallelise |
| `.ts` classified as Code | TypeScript wins over MPEG-2 Transport Stream by design; use Include filter for video `.ts` |
| macOS Trash timeout | `osascript` has a 15-second timeout; very large files may fall back to permanent delete |
| Tkinter HiDPI on Windows | May appear blurry on 4K displays without a DPI-aware manifest in the `.exe` |

---

## 👤 Author

<div align="center">

**Abhishek Srivastava**

*Software Developer · Python · Systems & Tooling*

[![LinkedIn](https://img.shields.io/badge/LinkedIn-Connect-0A66C2?style=for-the-badge&logo=linkedin&logoColor=white)](http://www.linkedin.com/in/abhishek-srivastava-1538461b1)

</div>

---

## 📄 License

This project is licensed under the **MIT License** — see the [LICENSE](./LICENSE) file for full terms.

You are free to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of this software, subject to the conditions in the LICENSE file.

---

<div align="center">

Built with ♥ and Python · stdlib only · no external dependencies

⬡ **DUPFINDER** · Enterprise Duplicate File Engine

</div>
