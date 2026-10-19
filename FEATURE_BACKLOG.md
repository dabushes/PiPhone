# PiPhone: Feature Backlog & Product Roadmap

This document outlines planned enhancements, architectural upgrades, and user experience features for future versions of **PiPhone**.

---

## 1. Backlog Items

### FB-01: "Find Next" Occurrence & Delta Analysis
* **Priority**: High (v1.1)
* **Description**: Allow users to continue searching $\pi$ beyond the first occurrence to find the 2nd, 3rd, or $N$-th occurrence of the same digit sequence.
* **Key Capabilities**:
  * Add a **"Find Next"** button to the results panel after an initial match is found.
  * Maintain an occurrence counter: `"Occurrence #2 at position #3,124,590 (+3,109,698 digits from previous)"`.
  * Display the distance / delta in digits between consecutive occurrences.
  * Plot occurrence interval distribution against the expected geometric distribution ($\mu = 10^L$).

### FB-02: Pre-computed Multi-Million Digit Seed Pack
* **Priority**: High (v1.2)
* **Description**: Provide an optional built-in downloader or bundled archive of a pre-computed 100,000,000-digit $\pi$ dataset.
* **Key Capabilities**:
  * For 7-digit US phone numbers, the probability of appearing in the first $10^7$ digits of $\pi$ is $\approx 63.2\%$, and in $10^8$ digits is $\approx 99.995\%$.
  * Background downloader with resume support, downloading a compressed (`zstandard` or `lz4`) digit pack.
  * Memory-mapped file I/O (`mmap`) enabling instantaneous zero-RAM queries across 100M digits.
  * Integrity checksum verification (SHA-256) on initial load.

### FB-03: Phone Number Formatting Toggle
* **Priority**: Medium (v1.1)
* **Description**: Provide an optional formatting toggle in the UI to display numbers in familiar telephone formats without affecting the underlying search engine.
* **Key Capabilities**:
  * Formats supported:
    * Standard US: `(NPA) NXX-XXXX` (10 digits) or `NXX-XXXX` (7 digits)
    * International E.164-style grouping
    * Raw unformatted digits (default)
  * Toggle switch in the header bar or settings menu.
  * Search history displays both formatted and unformatted forms.

### FB-04: Export / Share Card Generator
* **Priority**: Medium (v1.2)
* **Description**: Generate a sleek, shareable visual card (PNG / SVG) showcasing where a user's phone number lives in the decimal expansion of $\pi$.
* **Key Capabilities**:
  * Card design features:
    * High-resolution dark-mode aesthetic matching the app theme.
    * Prominently highlighted number within the $\pi$ stream.
    * Exact decimal digit position index and discovery date.
    * QR code linking to an online mathematical verification tool.
  * One-click "Copy Image to Clipboard" or "Save Image As...".

### FB-05: Telephony Audio Sonification & DTMF Dial Tones
* **Priority**: Low (v1.3)
* **Description**: Add authentic Dual-Tone Multi-Frequency (DTMF) dial tones when clicking keypad buttons and audio playback of the matched sequence in $\pi$.
* **Key Capabilities**:
  * Synthesize standard ITU-T DTMF frequencies (e.g., Key `1`: 697 Hz + 1209 Hz) via lightweight `wave` / `winsound` / `numpy` synthesis.
  * Audio preview button to "listen" to the flanking $\pi$ digits converted to pitch frequencies.
  * Mute / volume slider in the header bar.

### FB-06: High-Performance GPU & C-Extension Acceleration
* **Priority**: Low (v2.0)
* **Description**: Integrate compiled C/Cython extensions or GPU compute backends to accelerate Chudnovsky series evaluation.
* **Key Capabilities**:
  * Leverage `gmpy2` binary wheels (GNU MP / MPFR / MPC) for 5x-10x throughput improvement over pure Python.
  * Multi-process chunk generation utilizing all CPU cores for deep 9- to 10-digit number searches.
  * Benchmarking suite comparing throughput across calculation backends.

### FB-07: Statistical & Frequency Analysis Dashboard
* **Priority**: Low (v2.0)
* **Description**: Add a third tab exploring statistical normality and randomness properties of the cached $\pi$ stream.
* **Key Capabilities**:
  * Digit frequency bar chart (uniform distribution check of `0-9`).
  * Chi-Square ($\chi^2$) goodness-of-fit test score.
  * Entropy calculation per 100,000-digit chunk.

---

## 2. Prioritization & Milestone Roadmap

```
v1.0 (Current MVP)
  +-- CustomTkinter Dark Mode Dialer UI (2-Tab layout)
  +-- 1-10 digit input validation & 11th digit block
  +-- Chudnovsky algorithm engine (mpmath)
  +-- Persistent raw stream (data/pi_digits.txt)
  +-- Timsort & Binary Search index (data/pi_index.json)
  +-- Start-of-Pi "3." context padding
  +-- Background chunk calculation & cancellation
  +-- Search History persistence

v1.1 (Immediate Follow-up)
  +-- FB-01: "Find Next" Occurrence & Delta Distance
  +-- FB-03: Phone Number Formatting Toggle
  +-- Audio DTMF feedback on dial pad clicks

v1.2 (Expansion & Sharing)
  +-- FB-02: Pre-computed 100M-digit Seed Pack Downloader with mmap
  +-- FB-04: Export / Share Card PNG Generator

v2.0 (High-Performance Engine)
  +-- FB-06: gmpy2 / Multi-process Chudnovsky acceleration
  +-- FB-07: Statistical Normalcy & Frequency Analyzer Tab
```

