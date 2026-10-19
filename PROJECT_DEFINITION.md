# PiPhone: Project Definition & Technical Architecture Specification

## 1. Executive Summary & Vision

**PiPhone** is an interactive desktop application combining a tactile, dark-mode telephone dialer interface (`customtkinter`) with an arbitrary-precision $\pi$ search engine. The premise is rooted in mathematical normality: in an infinite, non-repeating, normal decimal expansion of $\pi$, every finite sequence of digits—including any 1- to 10-digit telephone number—is guaranteed to appear.

PiPhone allows users to dial or enter any phone number or digit sequence up to 10 digits and locate its exact 1-based decimal position within $\pi$. To deliver a responsive user experience across deep decimal expansions, PiPhone employs:
* A fast-converging **Chudnovsky algorithm** calculation engine implemented in `mpmath` (with optional `gmpy2` hardware acceleration).
* A persistent disk cache of generated digits (`data/pi_digits.txt`).
* A two-tiered sorted index leveraging Python's **Timsort** and **Binary Search** (`bisect`) for sub-millisecond repeated queries.
* Asynchronous chunked calculation in background worker threads with boundary overlap checking and cooperative thread cancellation.

---

## 2. User Interface & Experience Architecture

The UI is built with `customtkinter` configured in dark mode (`"Dark"`, theme `"blue"`), presenting a modern, distraction-free telephony interface.

```
+-------------------------------------------------------------+
| PIPHONE                                           * Ready   |
+-------------------------------------------------------------+
|  [ Pi Dialer ]    [ Search History ]                        |
| +---------------------------------------------------------+ |
| |                    [  8675309  ]                        | |
| |                                                         | |
| |     [  1  ]             [  2  ]             [  3  ]     | |
| |     [  4  ]             [  5  ]             [  6  ]     | |
| |     [  7  ]             [  8  ]             [  9  ]     | |
| |     [ CLR ]             [  0  ]             [  <- ]     | |
| |                                                         | |
| |            [       Find in Pi       ]                   | |
| |                                                         | |
| |   Result:                                               | |
| |            9245   8675309   1239                        | |
| |   Found at decimal digit #9,213,421                     | |
| |   Source: Calculated 200,000 new digits (1.2s)          | |
| +---------------------------------------------------------+ |
+-------------------------------------------------------------+
```

### 2.1 Two-Tab Layout
The former three-tab layout (`Dialer`, `Contacts`, `History`) is replaced by a focused two-tab interface:
1. **Pi Dialer**: The core interactive keypad, input display, search controls, and visual match context display.
2. **Search History**: A searchable, scrollable log of all queries performed in the current session and historical sessions.
*(The legacy `Contacts` tab is completely removed).*

### 2.2 Input Field & Digit Sanitization
* **Allowed Characters**: Numeric digits `0-9` only.
* **Length Bounds**: 1 digit minimum, **10 digits maximum** (standard North American / international phone number without country code).
* **11th Digit Enforcement**:
  * Keypad clicks are blocked when `len(current_input) >= 10`.
  * Keyboard typing and clipboard pasting events are intercepted via a validation callback (`validate="key"`). Any insertion causing `len > 10` or containing non-digits (`[^0-9]`) is rejected immediately.
* **Visual Styling**: Centered `CTkEntry` with large, prominent typography (`24pt`, bold) and subtle placeholder (`"Enter 1-10 digits..."`).

### 2.3 Keypad Layout & Custom Action Keys
The dial pad follows the standard 3x4 layout with repurposed telephony control keys:
* Row 1: `[1]`, `[2]`, `[3]`
* Row 2: `[4]`, `[5]`, `[6]`
* Row 3: `[7]`, `[8]`, `[9]`
* Row 4:
  * Bottom-Left: **Clear (`CLR`)** — Replaces `*`. Instantly clears the input field and resets match displays.
  * Bottom-Center: `[0]` — Standard digit.
  * Bottom-Right: **Backspace (`<-` / `DEL`)** — Replaces `#`. Deletes the trailing character.

### 2.4 Primary Action Button & State Transformation
* **Default State**: Prominent green button labeled **Find in Pi** (`fg_color="#2E7D32"`, `hover_color="#1B5E20"`).
* **Disabled State**: When the input field is empty, the button is disabled (`state="disabled"`).
* **Active Calculating State**: When a search triggers background $\pi$ expansion, the button dynamically transforms into a bold red **Cancel** button (`fg_color="#C62828"`, `hover_color="#B71C1C"`, text `"Cancel Search"`).
  * Clicking **Cancel** signals the background calculation thread via a `threading.Event`, safely halts chunk generation, preserves all computed digits to disk, and resets the UI state.

### 2.5 Visual Result Display & Contextual Ellipses
When a match is found, the target sequence is displayed flanked by its surrounding decimal context with clear font size hierarchy and contextual ellipses:

```
[ ...Preceding 4 Digits ]   [ TARGET MATCHED DIGITS ]   [ Following 4 Digits... ]
      (14pt, Muted)               (28pt, Bold Accent)          (14pt, Muted)
```

#### Exact Start-of-Pi Rule & Ellipsis Formatting
* Position `1` is defined as the **first digit after the decimal point** (`1` in `3.1415926535...`).
* **Standard Case (Position >= 5)**: Prepend leading `...` to the 4 preceding digits and append trailing `...` to the 4 following digits:
  * **Position 5+** (`9265`): Preceding = `...1415`, Target = `9265`, Following = `3589...`
* **Start-of-Pi Edge Case (Positions 1 through 4)**: When a match occurs within the first 4 decimal digits, the integer part `"3."` is included in the preceding context. Crucially, the **leading `...` is omitted** before `"3."` because nothing precedes the decimal point in $\pi$:
  * **Position 1** (`1415`): Preceding = `3.`, Target = `1415`, Following = `9265...`
  * **Position 2** (`4159`): Preceding = `3.1`, Target = `4159`, Following = `2653...`
  * **Position 3** (`1592`): Preceding = `3.14`, Target = `1592`, Following = `6535...`
  * **Position 4** (`5926`): Preceding = `3.141`, Target = `5926`, Following = `5358...`

### 2.6 Live Telemetry & Status Panel
* **Searching State**: Live real-time counter updated continuously:
  `"Calculating Pi... 1,400,000 digits searched (2.3s elapsed)"`
* **Found State**:
  * Position: `"Found at decimal digit #14,892"`
  * Latency & Source:
    * Cache Hit: `"Source: Sorted Index Cache (0.2ms)"`
    * Calculated: `"Source: Calculated 100,000 new digits (1.4s)"`
* **Cancelled State**: `"Search cancelled by user."`

### 2.7 Search History Tab & Total Cached Digits Banner
* **Total Digits Telemetry Banner**: Prominent cyan card (`#00E5FF`) at the top of the history tab displaying:
  **`Total Pi Digits Cached: X,XXX,XXX`**
  Automatically updates when new digits are calculated or when switching to the History tab.
* **Scrollable History Feed**: A scrollable table (`CTkScrollableFrame`) displaying historical searches:
  * Columns: Searched Digits | Found Position | Context Snippet | Latency | Source | Timestamp.
  * Storage: Persisted on disk at `data/search_history.json`.
  * Actions: One-click "View" button to reload any past match back into the main dialer display.

### 2.8 Keyboard & Numpad Input Bindings
Full keyboard navigation is enabled whenever the **Pi Dialer** tab is active:
* **Numeric Keys `0`–`9` & Numpad**: Appends digit to the dialer input (enforces 10-digit limit and prevents double-entry when `CTkEntry` has focus).
* **`Return` / `KP_Enter`**: Initiates **Find in Pi** (if input length is 1–10 digits and not searching).
* **`BackSpace`**: Deletes trailing digit (`⌫`).
* **`Escape` / `Delete`**: Clears entire input field (`CLR`).

### 2.9 Interactive Checkpoint Pause Modal (10k / 100k Stepping)
When a search misses the existing cache and calculates new digits, the engine pauses after every **10,000 newly calculated digits** (default step size):
* **Modal Dialog (`CTkToplevel`)**: Centered over main window, blocking main window interactions (`grab_set()`).
* **Message**: *"Match not found yet. Calculated X,XXX new digits (Total Pi Digits Cached: Y,YYY,YYY). Would you like to continue searching?"*
* **Three Actions**:
  1. **`Continue (+10,000)`**: Calculates next 10,000 digits before pausing again.
  2. **`Continue (+100,000)`**: Dynamically switches step size to 100,000 digits before pausing again.
  3. **`Stop Search`**: Halts search, saves all computed digits and index entries to disk, updates the History tab banner, and resets UI state.

---

## 3. Algorithmic & Storage Architecture

```mermaid
flowchart TD
    A[User Enters Digits 1-10] --> B[Click 'Find in Pi']
    B --> C{Binary Search in Query Cache?}
    C -- Hit --> D[Retrieve 1-Based Position & Return Context]
    C -- Miss --> E{Check 4-Digit Prefix Index}
    E -- Prefix Found in Cached Digits --> F[Scan Candidate Offsets in Cached pi_digits.txt]
    F -- Full Match Found --> G[Record to Query Index using Timsort]
    F -- No Match in Cache --> H[Launch Background Worker Thread]
    E -- Prefix Not in Cache --> H
    H --> I[Chudnovsky Expansion: 100k Digits Chunk]
    I --> J[Check Overlap with Previous Chunk]
    J --> K{Sequence Found in New Chunk?}
    K -- Yes --> L[Append Digits to data/pi_digits.txt]
    L --> M[Update Sorted Index via Timsort]
    M --> N[Notify UI via Thread-Safe Queue]
    K -- No --> O{User Clicked Cancel?}
    O -- Yes --> P[Clean Cancellation & Persist Digits]
    O -- No --> I
```

### 3.1 Pi Calculation Engine (Chudnovsky Algorithm)
The calculation engine uses the Ramanujan-Chudnovsky hypergeometric series:

$$\frac{1}{\pi} = 12 \sum_{k=0}^{\infty} \frac{(-1)^k (6k)! (545140134k + 13591409)}{(3k)! (k!)^3 (640320)^{3k + 3/2}}$$

* **Convergence Rate**: ~14.181647 decimal digits per term.
* **Precision Control**: Configured through `mpmath.mp.dps` with appropriate guard digits to eliminate boundary roundoff errors.
* **Fallback & Optimization**: Primary calculation implemented using Python's high-precision `mpmath`. If the optional C-accelerated library `gmpy2` is installed in the environment, it is automatically leveraged for greater computational throughput.

### 3.2 Persistent Raw Stream (`data/pi_digits.txt`)
* **Format**: Plain ASCII text file containing contiguous decimal digits of $\pi$ directly following `3.`.
  * Index offset 0 in the file corresponds to 1-based decimal digit position 1 (`1` in `14159...`).
* **Chunked Appending**: Newly computed digits are appended in 100,000-digit blocks.
* **Zero Corruption Guarantee**: Writes are flushed and fsynced. On application startup, the file length determines the starting baseline precision.

### 3.3 Sorted Index & Lookup Engine (`data/pi_index.json`)
The application maintains two internal lookup tables:
1. **Query Cache**: A sorted list of `[digit_sequence, first_occurrence_1_based_index]` tuples for every previously resolved user query.
2. **Prefix Index**: A sorted index of 4-digit prefixes mapping to their first known occurrence or candidate offsets in the cached decimal stream.

#### Sorting Algorithm: Timsort
Whenever new items are added to the query cache or prefix index:
* Python's native **Timsort** (`list.sort()` / `sorted()`) is executed.
* Timsort's $O(n)$ best-case on nearly-sorted data ensures high efficiency when merging incremental batches of newly discovered positions.

#### Search Strategy Pipeline
1. **Stage 1 (Query Cache Lookup - $O(\log n)$)**:
   * Perform binary search (`bisect.bisect_left`) against the sorted query cache.
   * If an exact match is present, resolve in $< 1\text{ms}$ with `Source: Sorted Index Cache`.
2. **Stage 2 (Prefix Index Jump - $O(\log k)$)**:
   * If the input length $\ge 4$, extract the leading 4-digit prefix.
   * Binary search the 4-digit prefix index to find candidate starting offsets in the cached `pi_digits.txt` stream.
   * Scan candidate offsets or scan the cached stream up to its existing length.
3. **Stage 3 (Chunked Asynchronous Expansion)**:
   * If the sequence is not present in existing cached digits, spawn a daemon `threading.Thread`.
   * Expand $\pi$ in discrete chunks of **100,000 digits**.
   * **Boundary Overlap Guarantee**: When scanning chunk $M$ against chunk $M-1$, inspect a window overlapping by `len(query) - 1` digits from the end of chunk $M-1$. This guarantees sequences straddling chunk boundaries (e.g. index 99,999 to 100,003) are never missed.
   * Continue until found or cancelled.

---

## 4. Concurrency & Thread-Safety Model

```
+------------------+         Queue.Queue          +-----------------------+
|  Tkinter Main UI | <--------------------------  | Calculation Thread    |
|  (60 FPS Loop)   |                              | (Chudnovsky Worker)   |
|                  |  --- threading.Event ---->   |                       |
+------------------+     (cancel_requested)       +-----------------------+
```

1. **Non-Blocking GUI**: All heavy arithmetic and disk searches run exclusively on background threads. The main Tkinter event loop remains responsive at 60 FPS.
2. **Thread-Safe Telemetry**: The background worker places periodic progress messages `(digits_searched, elapsed_seconds)` into a `queue.Queue`. The UI polls this queue every 50ms using `self.after(50, self._poll_search_queue)`.
3. **Cooperative Cancellation**: When the user clicks **Cancel**, a `threading.Event` (`cancel_event.set()`) is flagged. The Chudnovsky chunk loop checks `cancel_event.is_set()` before starting each term iteration and chunk write. On abort, it writes the completed digits and terminates gracefully.

---

## 5. Directory Structure & File Layout

```
PiPhone/
|-- data/
|   |-- pi_digits.txt          # Raw ASCII stream of calculated pi digits (excluding "3.")
|   |-- pi_index.json           # Persisted sorted index (queries + prefix offsets)
|   +-- search_history.json     # Search history log
|-- tests/
|   |-- __init__.py
|   +-- test_pi_engine.py       # Full pytest suite covering TC-01 through TC-07
|-- piphone.py                  # Main CustomTkinter UI application
|-- pi_engine.py                # Core calculation, indexing, and search engine
|-- PROJECT_DEFINITION.md       # Complete system architecture and spec
|-- TEST_PLAN.md                # Comprehensive test cases and verification matrix
|-- FEATURE_BACKLOG.md          # Future roadmap and prioritized backlog
|-- requirements.txt            # Package dependencies
+-- requirements.md             # Dependency documentation
```

