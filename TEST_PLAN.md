# PiPhone: Test Design & Verification Plan

## 1. Test Strategy Overview

The PiPhone test plan validates the accuracy of the mathematical calculations, string slicing edge cases, disk persistence, sorting and lookup algorithms, and concurrent worker execution.

The verification harness uses **`pytest`** (version `>=8.0.0`) organized into modular fixtures. Tests are fully isolated from production files by utilizing temporary directories (`tmp_path`) for disk-backed persistence during test runs.

### Test Categories
* **Unit Tests**: Chudnovsky algorithmic accuracy, input sanitizer/validator functions, context formatting utilities.
* **Algorithmic Indexing Tests**: Timsort ordering, `bisect` binary search accuracy, and index serialization.
* **Boundary & Edge Case Tests**: Overlap matching across 100,000-digit chunk boundaries, start-of-$\pi$ prefix formatting with `"3."`.
* **Concurrency & State Tests**: Worker thread cooperative cancellation via `threading.Event`, queue telemetry emission, and state resets.

---

## 2. Test Cases Matrix

| Test ID | Name | Objective | Input / Action | Expected Result |
| :--- | :--- | :--- | :--- | :--- |
| **TC-01** | Start-of-Pi Context (`1415`) | Verify position 1 detection and `"3."` preceding padding when no preceding decimal digits exist. | Query = `"1415"` | Pos: `1`<br>Preceding: `"3."`<br>Target: `"1415"`<br>Following: `"9265"` |
| **TC-02** | Partial `3.` Padding (`4159`) | Verify context padding when only 1 decimal digit precedes target. | Query = `"4159"` | Pos: `2`<br>Preceding: `"3.1"`<br>Target: `"4159"`<br>Following: `"2653"` |
| **TC-03** | Standard 4-Digit Match & Context (`9265`) | Verify normal 4-digit preceding and following context when $>4$ digits precede target. | Query = `"9265"` | Pos: `5`<br>Preceding: `"1415"`<br>Target: `"9265"`<br>Following: `"3589"` |
| **TC-04** | Input Validation & 11th Digit Block | Verify inputs 1–10 digits accepted, non-digits rejected, 11th digit blocked. | Test valid strings (`"7"`, `"8675309"`, `"1234567890"`), invalid (`"12a"`, `""`, `"-5"`), and 11-digit (`"12345678901"`). | Valid: Accepted.<br>Non-numeric: Rejected.<br>Len > 10: Blocked at 10 chars. |
| **TC-05** | Timsort & Binary Search Cache Hit | Verify duplicate query resolves via binary search (`bisect`) without generating new $\pi$ chunks. | Search `"9265"` twice; assert calculation count on 2nd query is 0. | 1st query: Calculated/Cached.<br>2nd query: `is_cache_hit = True`, latency $< 2\text{ms}$, 0 new digits generated. |
| **TC-06** | Chunk Boundary Overlap | Verify sequence straddling the boundary between chunk $N$ and chunk $N+1$ is accurately detected. | Synthesize or compute across chunk boundary (e.g., at digit `100,000`), search sequence spanning digits `99,998` to `100,003`. | Match located at the boundary index without missing digits or duplication. |
| **TC-07** | Background Thread Cancellation | Verify setting `cancel_event` cleanly halts computation, retains computed digits, and resets UI state. | Launch background search for deep unknown sequence; trigger `cancel_event.set()` after 100ms. | Worker thread exits safely within 500ms; calculated digits up to cancellation point are intact on disk. |
| **TC-08** | Chudnovsky Mathematical Precision | Verify computed digits against mathematical ground truth constants. | Compute first 1,000 digits of $\pi$. | Decimal sequence matches reference sequence of $\pi$ exactly with 0 discrepant digits. |
| **TC-09** | Persistent Cache & Cold Start | Verify saving calculated digits and index to disk, re-instantiating engine, and finding sequence. | Compute digits, persist to disk, instantiate new `PiEngine` instance from same directory. | Engine loads cached stream and sorted index immediately on startup without recalculation. |
| **TC-10** | Single-Digit Edge Case Search | Verify searching single-digit inputs (e.g. `"1"`, `"4"`, `"9"`) locates correct first 1-based decimal position. | Query = `"1"`, Query = `"4"` | `"1"` -> Pos 1, Preceding: `"3."`, Target: `"1"`, Following: `"4159"`<br>`"4"` -> Pos 2, Preceding: `"3.1"`, Target: `"4"`, Following: `"1592"` |

---

## 3. Detailed Test Specification

### 3.1 Start-of-Pi Context Padding (TC-01, TC-02, TC-03, TC-10)
Let $P$ be the 1-based decimal position of the target sequence within $\pi = 3.d_1 d_2 d_3 \dots$:
* For $P = 1$: Preceding context is `"3."`
* For $P = 2$: Preceding context is `"3." + d_1` (length 3, starting with `"3."`)
* For $P = 3$: Preceding context is `"3." + d_1 d_2` (length 4, starting with `"3."`)
* For $P = 4$: Preceding context is `"3." + d_1 d_2 d_3` (length 5, starting with `"3."`)
* For $P \ge 5$: Preceding context is $d_{P-4} d_{P-3} d_{P-2} d_{P-1}$ (exactly 4 preceding decimal digits).
* Following context is always the 4 subsequent decimal digits: $d_{P+L} d_{P+L+1} d_{P+L+2} d_{P+L+3}$ where $L = \text{len}(\text{query})$.

### 3.2 Input Validation Specification (TC-04)
The validation logic `validate_query(input_str)` must conform to:
$$\text{valid}(s) \iff s \in \{0, 1, \dots, 9\}^L \quad \text{where} \quad 1 \le L \le 10$$
* Strings containing spaces, symbols (`+`, `-`, `*`, `#`), or letters must raise `ValueError` or return `False`.
* Inputs longer than 10 characters must be truncated or rejected.

### 3.3 Timsort and Bisect Cache Verification (TC-05)
* Queries are stored as `(query_digits, 1_based_position)`.
* The collection is maintained in sorted order using Python's native `list.sort()` (Timsort).
* Lookup uses `bisect.bisect_left(index_keys, query)` in $O(\log n)$ time.
* If found, the engine must return `source="Sorted Index Cache"` without invoking any `mpmath` functions.

### 3.4 Boundary Overlap Logic (TC-06)
When expanding in chunks of size $C$ (e.g. $C = 100,000$):
* Chunk $k$ covers decimal slice $[k \cdot C + 1, (k+1) \cdot C]$.
* To ensure no missed sequence of length $L \le 10$ that starts at position $k \cdot C - L + 2$ and ends in chunk $k+1$, the search buffer must concatenate:
  $$\text{Search Window} = \text{PreviousChunk}[- (L - 1):] + \text{NewChunk}$$
* The offset mapping must correctly adjust match indices back to the global 1-based position.

### 3.5 Cancellation Verification (TC-07)
* Tests must initiate a background thread search with a provided `threading.Event()`.
* Within a short duration, `cancel_event.set()` is called.
* The test verifies `thread.is_alive()` becomes `False` within $< 1.0\text{s}$, and no thread leak occurs.

---

## 4. Test Execution Guidelines

Run the test suite using `pytest` from the virtual environment:

```bash
# Run all tests with standard verbose output
.\venv\Scripts\pytest -v

# Run a specific test case
.\venv\Scripts\pytest -v tests/test_pi_engine.py -k "test_tc01"

# Run tests with execution timing
.\venv\Scripts\pytest -v --durations=10
```

