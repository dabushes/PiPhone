# Security Policy

## Overview

**PiPhone** is an arbitrary-precision mathematical decimal search engine and phone-number lookup application. Security, memory safety, and file integrity are core design priorities. This document details PiPhone's security posture, threat model, safe computing practices, and vulnerability reporting procedures.

---

## Supported Versions

| Version | Supported | Security Review Date | Status |
| :--- | :---: | :---: | :--- |
| **v2.4.x** | :white_check_mark: | October 2026 | Active / Current Release |
| **v2.3.x** | :white_check_mark: | October 2026 | Maintenance Mode |
| **v2.2.x** | :x: | — | End of Life (Upgrade to v2.4+) |
| **v2.1.x** | :x: | — | End of Life (Upgrade to v2.4+) |
| **v1.x**   | :x: | — | End of Life (Upgrade to v2.4+) |

---

## Security Architecture & Threat Model

### 1. Local File I/O & Path Traversal Prevention
* **Directory Sandboxing:** All local persistence (`data/`, `seed/`, history files) is resolved strictly relative to validated application directories using `os.path.abspath` and canonical path resolution.
* **Seed Import Validation & Atomic Staging:** Seed imports (`import_seed`) strictly validate that files start with the canonical 10-digit Pi prefix (`1415926535`) and contain exclusively ASCII decimal digits (`'0'`–`'9'`). Validated streams are staged to an isolated temporary `.tmp` file before an atomic `os.replace()` swap, ensuring the active digits cache is never overwritten or corrupted in-place by malformed or interrupted transfers.
* **Crash-Safe Atomic Checkpointing:** Index builder state and bitset checkpoints (`state.ckpt.json`, `bitset.ckpt.bin`) are staged via temporary files before atomic filesystem promotion, preventing corrupted partial states during abrupt interruptions.
* **Binary Bundle Isolation:** When packaged as a PyInstaller frozen binary (`getattr(sys, 'frozen', False)`):
  * Read-only runtime assets are accessed exclusively from `sys._MEIPASS`.
  * User-modifiable and persisted state (`data/`, `dist/seed/`) is sandboxed next to `sys.executable`.
* **Safe Archive Decompression:** Compressed seed archives (`.zst`, `.xz`, `.gz`) are decompressed with strict format decoding and exception handling to prevent decompression bomb or memory denial of service.

### 2. Memory & Buffer Bounds Safety
* **uint40 Little-Endian Encoding:** The `.piidx` binary index architecture stores 10-digit phone queries and decimal positions using 5-byte unsigned little-endian integers (`uint40 LE`). This enforces mathematical bounds of $2^{40} - 1 \approx 1,099,511,627,775$ ($1.09$ trillion digits), preventing integer rollover or memory corruption on telephone sequences up to 1 trillion digits.
* **Read-Only Memory-Mapped File I/O (`mmap`):** Binary index lookups use `mmap.mmap(..., access=mmap.ACCESS_READ)`. Operating system kernel page faulting guarantees that:
  * Application memory is bounded (typically $<120\text{ MB}$ RAM even when querying a 50 GB index).
  * Index files are immutable at runtime; corrupting writes from the lookup engine are impossible.
* **Strict Payload Bounds Verification:** Binary index loaders enforce strict payload alignment against record sizes (10 bytes per Mode 1 straggler record, exact table bounds for Mode 2) and automatically release file descriptors on corrupt or truncated headers.
* **Bitset Memory Management:** Temporary bitsets used during index compilation ($1.25\text{ GB}$ for $10^{10}$ bits) are immediately deallocated (`bytearray(0)`) followed by explicit garbage collection (`gc.collect()`) upon pipeline completion to prevent persistent RAM exhaustion.
* **Safe-RAM Index Miss Guard:** On index misses beyond $100\text{M}$ digits, unbounded live calculation bursts are intercepted and blocked by default, prompting the user for explicit confirmation before allocating large RAM buffers.

### 3. Concurrency, GIL & Subprocess IPC Security
* **Process Isolation for Heavy Math:** Heavy Chudnovsky calculations beyond the seed cache execute in dedicated child processes via `multiprocessing.Process`. This ensures:
  * The main UI thread and GIL remain unblocked and fully responsive.
  * User cancellations trigger immediate operating system termination (`process.terminate()`, `process.kill()`) with zero orphaned background workers or leaking arithmetic buffers.
* **Thread-Safe UI Communication:** All background threads communicate with Tkinter exclusively through synchronized `queue.Queue` channels polled on the main event loop (`root.after(50, ...)`), preventing Tkinter cross-thread race conditions.

### 4. Input Sanitization & Format Validation
* **Strict ASCII-Digit Filtering:** User input, phone lookup strings, and seed digits are validated against strict ASCII decimal digit constraints (`c.isascii() and c.isdigit()`). Non-ASCII Unicode numerals (e.g., Arabic-Indic `٠-٩`, superscript `²`, or fullwidth digits) and control codes are rejected at the parsing boundary, preventing arithmetic skew in rolling integer windows.
* **Header & Magic Number Verification:** Index files are verified against a 5-byte magic signature (`b"PI10\x01"`). Malformed, truncated, or incompatible files are safely rejected without buffer underflows or unhandled exceptions.

---

## Reporting a Vulnerability

If you discover a security vulnerability or memory leak in PiPhone, please do NOT file a public issue. Follow these steps:

1. **Email Notification:** Send an email to `security@piphone.local` (or file a private GitHub Security Advisory).
2. **Include Details:**
   - Affected PiPhone version (e.g., v2.4).
   - Operating system and Python version.
   - Minimal reproduction script or input sequence triggering the anomaly.
   - Crash dumps or error stack traces if applicable.
3. **Response Timeline:** We aim to acknowledge reports within **48 hours** and provide a patch or mitigation within **7 days**.

