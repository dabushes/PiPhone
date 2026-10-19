# PiPhone

**PiPhone** is an interactive desktop application combining a sleek, dark-mode telephone dialer interface (`customtkinter`) with an arbitrary-precision decimal search engine for $\pi$. Search for any 1- to 10-digit sequence or phone number and find its exact position within the decimal expansion of $\pi$.

---

## Features

| Feature | Description |
|---------|-------------|
| **Pi Dialer Tab** | Keypad (`0–9`, `CLR`, `⌫`), numeric input (1–10 digits with strict 11th-digit block), and prominent action controls. |
| **Search in Pi** | Locates user-entered digit sequences in $\pi$ using a fast Chudnovsky algorithm engine (`mpmath`). |
| **Visual Match Context** | Flanking display with 4 preceding digits (or `3.` start-of-$\pi$ padding), highlighted target sequence, and 4 following digits. |
| **Sorted Index & Cache** | Two-tiered lookup table maintained via Python's native Timsort and $O(\log n)$ Binary Search (`bisect`) for instant repeated queries. |
| **Background Expansion** | Dynamically expands $\pi$ in 100,000-digit background chunks with boundary overlap detection and live telemetry. |
| **Search Cancellation** | Cooperative cancellation via `threading.Event`, allowing deep searches to be cancelled immediately while preserving computed digits. |
| **Search History Tab** | Persistent scrollable log of all queries, match positions, context snippets, and execution sources. |

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
