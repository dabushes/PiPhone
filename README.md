# PiPhone

**PiPhone** is an interactive desktop application combining a sleek, dark-mode telephone dialer interface (`customtkinter`) with an arbitrary-precision decimal search engine for $\pi$. Search for any 1- to 10-digit sequence or phone number and find its exact position within the decimal expansion of $\pi$.

---

## Features

| Feature | Description |
|---------|-------------|
| **Dynamic Seed Expansion** | Dropdown selector (`100M`, `500M`, `1B` digits) with confirmation prompts, memory/time estimates, and background async expansion for deep offline caches. |
| **Safe-RAM Index Miss Guard** | Catches index and seed misses gracefully without blocking or unbounded live calculation, prompting the user with non-blocking expansion and live calculation options. |
| **Screen Reader & a11y Live Regions** | Full accessibility support with `AriaAnnouncementLabel` emitting live-region updates to Windows Narrator & NVDA, plus keyboard navigation (`Ctrl+F`, `Return`, `Tab`). |
| **About & Architecture Screen** | Interactive dialog (`ⓘ About`) explaining Ramanujan-Chudnovsky math, Coupon Collector's theorem ($E[T] \approx 236.4\text{B}$ digits), memory-mapped `.piidx` architecture, and hardware benchmarks. |
| **Security & Memory Safety** | Formal security policy ([`SECURITY.md`](SECURITY.md)) auditing local file I/O sandboxing, uint40 LE buffer bounds, and process-isolated subprocess IPC. |
| **Modular Engine Selector** | Segmented button on the Dialer tab allowing instantaneous toggling between `Standard Live` calculation and `Binary Index (.piidx)`. |
| **10-Digit Binary Index Engine** | Microsecond ($< 1\text{ms}$) lookups via memory-mapped `.piidx` files with dual-mode support (Mode 1: sorted 10-byte stragglers; Mode 2: $O(1)$ direct 5-byte array). |
| **Side-by-Side Comparison Mode** | `Compare Both` checkbox running both search engines back-to-back, displaying relative latencies and the speedup factor ($\text{X.X}\times$ faster ⚡). |
| **Index Manager Dialog** | Built-in GUI manager (`⚙️ Index`) to inspect active index metadata, load/unload `.piidx` files, or build new indices with real-time throughput metrics. |
| **Streaming Index Builder** | High-performance CLI and GUI builder (`pi_index_builder.py`) featuring 1.25 GB bitset tracking ($10^{10}$ bits), 9-digit boundary stitching, and atomic crash checkpoints. |
| **Pi Dialer Tab** | Keypad (`0–9`, `CLR`, `⌫`), numeric input (1–10 digits with strict 11th-digit block), and full keyboard/numpad shortcuts. |
| **Smartphone Dialer Form Factor** | Sleek 390x720 aspect ratio, tactile phone-proportioned keypad (`corner_radius=22`, `22pt bold`), and dynamic target font scaling (26pt/22pt/19pt) preventing horizontal clipping. |
| **Bundled 100M Seed Pack** | Bundled compressed 100,000,000-digit $\pi$ seed pack (`seed/pi_100m.zst`, 40.56 MB) enabling instantaneous (< 20ms) resolution for 1-to-7 digit numbers right out of the box. |
| **Seed Pack Export & Import** | One-click `📤 Export Seed` and `📥 Import Seed` buttons on the Search History tab supporting `.zst`, `.gz`, `.xz`, and `.txt` formats with ground-truth validation. |
| **Calibrated 100M Time Estimates** | Scaled with empirical exponent $1.28$ and baseline calibration (100M digits / 92.0s) persisted in `pi_index.json`, providing accurate `~2 Mins` estimates without cold-start overshoot. |
| **IPC Delta Slice Optimization** | Subprocess calculation transfers only the newly computed slice (~2 MB) rather than rewriting the full 100M+ stream, streaming writes directly to disk. |
| **Visible, Expandable Local Seed** | External visible seed archive in `dist/seed/` next to `PiPhone.exe` that automatically expands and updates on disk whenever new digits are computed beyond 100M. |
| **Mouse-Selectable Text (`Ctrl+C`)** | Borderless selectable result and telemetry widgets allowing users to mouse drag-select or double-click any context digits, target matches, or position numbers and press `Ctrl+C` to copy them. |
| **Immediate Modal on 100M Miss** | When a query misses the 100M cache, pops up the Checkpoint Modal immediately (< 0.05s) rather than freezing on silent multi-minute background calculation. |
| **Instant Subprocess Cancel (< 0.05s)** | Precision calculation runs in a child process, terminating immediately on Cancel via OS-level process kill, accompanied by an animated progress indicator. |
| **Hardware-Accelerated Math** | High-throughput Chudnovsky engine powered by `mpmath` and compiled C `gmpy2` (10x–15x speedup). |
| **1M-Digit Initial Burst** | Searches missing the cache automatically calculate up to 1,000,000 digits in the initial burst before the first checkpoint modal. |
| **Time-Budget Calculation Engine** | True wall-clock timer loop continuously expanding and searching $\pi$ in real-time adaptive chunks until the time budget (30s, 60s, 120s, 300s) is exhausted or the sequence is found. |
| **$T_{\text{min\_step}}$ Golden Rule (5 Tiers)** | Enforces the Golden Rule ($T_{\text{budget}} \ge 1.25 \times T_{\text{min\_step}}$) with cold-start CPU micro-benchmarking, escalating through 5 tiers up to Deep Pi / 100M+ mode. |
| **Boundary Stitch & Lookahead Safety** | Hardened `len(query) - 1` boundary stitch, incremental 4-digit prefix indexing, and end-of-chunk guard digits ensuring all 4 context digits are always available. |
| **Visual Match Context** | Distinct font hierarchy (14pt muted vs 28pt bold green accent) with contextual ellipses (`...1234` **`5678`** `9012...`) and Start-of-Pi `3.` padding. |
| **Sorted Index & Cache** | Two-tiered lookup table maintained via Python's native Timsort and $O(\log n)$ Binary Search (`bisect`) for instant repeated queries. |
| **Total Cached Digits Banner** | Real-time telemetry banner on Search History tab showing `Total Pi Digits Cached: X,XXX,XXX`. |
| **Search Cancellation** | Cooperative cancellation via `threading.Event`, allowing deep searches to be cancelled immediately while preserving computed digits. |
| **Search History Tab** | Persistent scrollable log of all queries, match positions, context snippets, and execution sources with one-click review. |

---

## Getting Started

### Prerequisites

- Python 3.8+
- Required dependencies listed in [`requirements.txt`](requirements.txt) / [`requirements.md`](requirements.md)

### Installation

1. Clone or download the repository.
2. Install dependencies:

```sh
pip install -r requirements.txt
```

### Running the App

Launch the application by running [`piphone.py`](piphone.py):

```sh
python piphone.py
```

### Building an Executable

To package the application into a standalone desktop executable with all bundled assets, native C extensions, and external visible seed:

```sh
# Canonical build command using the hardened PyInstaller spec configuration
# (Ensures all hidden imports such as zstandard, gmpy2, and customtkinter assets are bundled)
pyinstaller PiPhone.spec

# Copy seed directory to dist/ for external distribution
python -c "import shutil, pathlib; (pathlib.Path('dist/seed')).mkdir(parents=True, exist_ok=True); shutil.copy2('seed/pi_100m.zst', 'dist/seed/pi_100m.zst')"
```
