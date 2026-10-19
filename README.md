# PiPhone

**PiPhone** is an interactive desktop application combining a sleek, dark-mode telephone dialer interface (`customtkinter`) with an arbitrary-precision decimal search engine for $\pi$. Search for any 1- to 10-digit sequence or phone number and find its exact position within the decimal expansion of $\pi$.

---

## Features

| Feature | Description |
|---------|-------------|
| **Pi Dialer Tab** | Keypad (`0–9`, `CLR`, `⌫`), numeric input (1–10 digits with strict 11th-digit block), and full keyboard/numpad shortcuts. |
| **Hardware-Accelerated Math** | High-throughput Chudnovsky engine powered by `mpmath` and compiled C `gmpy2` (10x–15x speedup). |
| **Visual Match Context** | Distinct font hierarchy (14pt muted vs 28pt bold green accent) with contextual ellipses (`...1234` **`5678`** `9012...`) and Start-of-Pi `3.` padding. |
| **Interactive Checkpoint Modal** | Pauses every 100,000 newly calculated digits with options for `+100,000` and `+1,000,000` stepping. |
| **Empirical Time Estimates** | Displays live estimated compute times on modal buttons (`[< 0.1s]`, `[~1.4s]`, `[~42s]`) via super-linear scaling law. |
| **60-Second Time-Ceiling Rule** | Automatically hides the `+1,000,000` button if its estimated compute duration exceeds 60 seconds to prevent runaway deep searches. |
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
