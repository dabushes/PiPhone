# PiPhone

**PiPhone** is an interactive desktop application combining a sleek, dark-mode telephone dialer interface (`customtkinter`) with an arbitrary-precision decimal search engine for $\pi$. Search for any 1- to 10-digit sequence or phone number and find its exact position within the decimal expansion of $\pi$.

---

## Features

| Feature | Description |
|---------|-------------|
| **Pi Dialer Tab** | Keypad (`0–9`, `CLR`, `⌫`), numeric input (1–10 digits with strict 11th-digit block), and full keyboard/numpad shortcuts. |
| **Bundled 100M Seed Pack** | Bundled compressed 100,000,000-digit $\pi$ seed pack (`seed/pi_100m.zst`, 40.56 MB) enabling instantaneous (< 20ms) resolution for 1-to-7 digit numbers right out of the box. |
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

To package the application into a standalone desktop executable using PyInstaller:

```sh
pyinstaller --noconsole --onefile piphone.py
```
