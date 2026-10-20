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
| **TC-01** | Start-of-Pi Context (`1415`) | Verify position 1 detection and `"3."` preceding padding without leading ellipsis. | Query = `"1415"` | Pos: `1`<br>Preceding: `"3."`<br>Target: `"1415"`<br>Following: `"9265..."` |
| **TC-02** | Partial `3.` Padding (`4159`) | Verify context padding when only 1 decimal digit precedes target without leading ellipsis. | Query = `"4159"` | Pos: `2`<br>Preceding: `"3.1"`<br>Target: `"4159"`<br>Following: `"2653..."` |
| **TC-03** | Standard 4-Digit Match & Context (`9265`) | Verify normal 4-digit preceding and following context with both leading and trailing ellipses. | Query = `"9265"` | Pos: `5`<br>Preceding: `"...1415"`<br>Target: `"9265"`<br>Following: `"3589..."` |
| **TC-04** | Input Validation & 11th Digit Block | Verify inputs 1–10 digits accepted, non-digits rejected, 11th digit blocked. | Test valid strings (`"7"`, `"8675309"`, `"1234567890"`), invalid (`"12a"`, `""`, `"-5"`), and 11-digit (`"12345678901"`). | Valid: Accepted.<br>Non-numeric: Rejected.<br>Len > 10: Blocked at 10 chars. |
| **TC-05** | Timsort & Binary Search Cache Hit | Verify duplicate query resolves via binary search (`bisect`) without generating new $\pi$ chunks. | Search `"9265"` twice; assert calculation count on 2nd query is 0. | 1st query: Calculated/Cached.<br>2nd query: `is_cache_hit = True`, latency $< 2\text{ms}$, 0 new digits generated. |
| **TC-06** | Chunk Boundary Overlap & Safety Lookahead | Verify 7-digit boundary straddle, terminal match lookahead extension, and incremental prefix indexing. | Search sequence straddling boundary (e.g. index 500), terminal boundary sequence (`4201989`), and check prefix index. | Match located with exact 1-based pos; all 4 following digits present; prefix index contains boundary prefixes. |
| **TC-07** | Background Thread Cancellation | Verify setting `cancel_event` cleanly halts computation, retains computed digits, and resets UI state. | Launch background search for deep unknown sequence; trigger `cancel_event.set()` after 20ms. | Worker thread exits safely within 500ms; calculated digits up to cancellation point are intact on disk. |
| **TC-08** | Ellipsis Formatting (Standard) | Verify that searching a sequence at position >= 5 formats preceding context with leading `...` and following with trailing `...`. | Query = `"9265"` at Pos 5 | Preceding: `"...1415"`, Target: `"9265"`, Following: `"3589..."` |
| **TC-09** | Ellipsis Formatting (Start-of-Pi `3.` Edge Case) | Verify that searching sequences at decimal positions 1–4 omits leading `...` before `3.` while retaining trailing `...`. | Query = `"1415"` (Pos 1), `"4159"` (Pos 2), `"1592"` (Pos 3), `"5926"` (Pos 4) | Pos 1: Preceding `"3."`<br>Pos 2: Preceding `"3.1"`<br>Pos 3: Preceding `"3.14"`<br>Pos 4: Preceding `"3.141"`<br>All with trailing `...` and no leading `...`. |
| **TC-10** | Checkpoint Pause & Resume Callback | Verify engine pauses at 100,000 new digits, can dynamically switch step size to 1,000,000, and cleanly saves state when stopped. | Search deep query with mock checkpoint callback switching to 1M then stopping. | Engine triggers callbacks at 100k and 1.1M, halts cleanly, and saves 1.1M digits to disk. |
| **TC-11** | Total Cached Digits Reporting | Verify engine accurately reports total count of cached decimal digits before and after chunk expansion. | Check `get_total_cached_digits()` before and after `_expand_to_length()`. | Accurate integer count returned matching disk file length across restarts. |
| **TC-12** | Empirical Time Estimation Scaling | Verify `estimate_chunk_time()` scaling law monotonicity and label formatting. | Test scaling law with variable total digits and deltas; verify bracketed format strings. | Estimates increase monotonically with target precision; formatting produces `[< 0.1s]`, `[~1.4s]`, `[~42s]`, `[~1.5m]`. |
| **TC-13** | 60-Second Time-Ceiling Rule | Verify engine flags `+1,000,000` option as visible when estimated time $\le 60\text{s}$ and hidden when $> 60\text{s}$. | Test `should_show_1m_step()` under fast baseline vs deep slow estimation states. | Returns `True` when $\le 60.0\text{s}$ and `False` when $> 60.0\text{s}$. |
| **TC-14** | Inverse Time-Budget Estimator | Verify `estimate_digits_for_time()` inverts `estimate_chunk_time()`, rounds to 100k, and enforces 100k floor. | Invert known chunk time; test edge cases with near-zero and negative budgets. | Inversion matches desired delta; returns exact multiples of 100k; minimum floor of 100k digits enforced. |
| **TC-15** | 5-Tier Escalating Checkpoint Schedule | Verify `get_checkpoint_tier_config()` across all 5 speed tiers based on $T_{\text{min\_step}}$. | Test with calibrated baseline metrics at $T_{\text{min\_step}} = 12.3\text{s}, 19.6\text{s}, 33.7\text{s}, 67.4\text{s}, 122.6\text{s}$. | Tier 1 & 2 offer `Continue (+1,000,000 Digits)` and timed bursts (30s / 60s); Tier 3 & 4 offer dual bursts; Tier 5 triggers Deep Pi block calculations and note. |
| **TC-16** | Multi-Step Cache Invariance | Verify computing Pi in 3 separate incremental steps (50k->100k->150k) is byte-for-byte identical to single-step 150k, and returns identical positions. | Compute 50k->100k->150k on one engine, 150k directly on another; assert `engine.pi_digits` match byte-for-byte and queries return identical positions. | Exact string equality (`assert inc.pi_digits == fresh.pi_digits`) and identical 1-based positions for boundary queries. |
| **TC-17** | Wall-Clock Time-Budget Loop | Verify choosing a time-budget option executes a wall-clock loop across multiple expansions until budget expires, updating telemetry. | Trigger 1.5s time-budget burst on unfound sequence; verify loop runs $\ge 1.1\text{s}$, cache grows across multiple iterations, and telemetry emits. | Wall-clock loop executes until deadline; persists expanded digits; emits progress messages; pauses with checkpoint modal when budget expires. |
| **TC-18** | $T_{\text{min\_step}}$ Golden Rule Enforcement | Verify that across all simulated $T_{\text{min\_step}}$ (5s, 20s, 35s, 65s, 180s), no time-budget button is ever shorter than $1.25 \times T_{\text{min\_step}}$, and Tier 5 triggers when $> 90\text{s}$. | Test tier configurations with simulated values; check modal buttons and durations. | Golden rule strictly satisfied across all tiers; all time-budget options $\ge 1.25 \times T_{\text{min\_step}}$; Tier 5 triggers note and block option. |
| **TC-19** | Seed Extraction & Landmark Verification | Verify bundled 100M seed decompression and instantaneous landmark searches. | Load seed into engine; search known landmarks `6411415` and `6434099`. | 100,000,000 digits loaded; `6411415` found at `23,018,978` and `6434099` found at `35,047,512` in $< 0.15\text{s}$. |
| **TC-20** | Chudnovsky Mathematical Precision | Verify computed digits against mathematical ground truth constants. | Compute first 50 digits of $\pi$. | Decimal sequence matches reference sequence of $\pi$ exactly with 0 discrepant digits. |
| **TC-21** | Persistent Cache & Cold Start | Verify saving calculated digits and index to disk, re-instantiating engine, and finding sequence. | Compute digits, persist to disk, instantiate new `PiEngine` instance from same directory. | Engine loads cached stream and sorted index immediately on startup without recalculation. |
| **TC-22** | Initial 1,000,000-Digit Burst | Verify that a cache miss when `current_total < 1,000,000` calculates up to 1,000,000 digits in the initial burst. | Search non-cached query with `chunk_size=None` starting from 100k cached digits. | First checkpoint fires at exactly 1,000,000 total digits (+900,000 newly calculated). |

---

## 3. Detailed Test Specification

### 3.1 Start-of-Pi Context & Ellipsis Rules (TC-01, TC-02, TC-03, TC-08, TC-09)
Let $P$ be the 1-based decimal position of the target sequence within $\pi = 3.d_1 d_2 d_3 \dots$:
* For $P = 1$: Preceding context is `"3."` (NO leading `...` because nothing precedes `3.`)
* For $P = 2$: Preceding context is `"3." + d_1` (NO leading `...`)
* For $P = 3$: Preceding context is `"3." + d_1 d_2` (NO leading `...`)
* For $P = 4$: Preceding context is `"3." + d_1 d_2 d_3` (NO leading `...`)
* For $P \ge 5$: Preceding context is `"..." + d_{P-4} d_{P-3} d_{P-2} d_{P-1}` (leading `...` prepended).
* Following context is always: $d_{P+L} d_{P+L+1} d_{P+L+2} d_{P+L+3} + \text{"..."}$ (trailing `...` appended).

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

