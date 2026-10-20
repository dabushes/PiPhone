# PiPhone: Feature Backlog & Product Roadmap

This document outlines completed milestones, planned enhancements, architectural upgrades, and user experience features for **PiPhone**.

---

## 1. Completed Features

### FB-02: Bundled 100,000,000-Digit Compressed Pi Seed Pack
* **Status**: **Completed (v2.0)**
* **Implementation**:
  * High-speed `gmpy2` / `mpmath.libmp.pi_fixed` generator producing 100,000,000 verified decimal digits of $\pi$.
  * Ground-truth verification against critical landmarks (`14159265` at pos 1, `6411415` at pos 23,018,978, and `6434099` at pos 35,047,512).
  * Compressed using `zstandard` (level 19) into `seed/pi_100m.zst` (40.56 MB), fitting safely within GitHub's 50 MB file limit.
  * Automatic first-launch decompression into `data/pi_digits.txt`, enabling instantaneous zero-wait resolution for all 1-to-7 digit numbers and ~63% of 8-digit numbers.

### FB-06: High-Performance C-Extension Hardware Acceleration (`gmpy2`)
* **Status**: **Completed (v1.2 & v2.0)**
* **Implementation**:
  * Integrated compiled C GMP/MPFR engine via `gmpy2` achieving 10x–15x throughput speedup over pure Python.
  * Verified active backend binding (`mpmath.libmp.BACKEND == 'gmpy'`).
  * Bundled seamlessly into PyInstaller standalone binary with native C DLLs.

### FB-GoldenRule: $T_{\text{min\_step}}$ Golden Rule for Dynamic Time-Budget Bursts
* **Status**: **Completed (v2.0)**
* **Implementation**:
  * Micro-benchmark calibration upon startup ensuring $N_{\text{last}}$ and $T_{\text{last}}$ match exact host CPU performance before estimating deep chunks.
  * Minimum step time calculation: $T_{\text{min\_step}} = \text{estimate\_chunk\_time}(\text{current\_total}, 2\_000\_000)$.
  * 5-Tier escalating modal schedule guaranteeing that time-budget buttons never offer a duration shorter than $1.25 \times T_{\text{min\_step}}$.
  * Dedicated Tier 5 Deep Pi mode for 100M+ digits with informative time warnings and block calculation options.

### FB-07: Visible, Expandable Local Seed Archive
* **Status**: **Completed (v2.1)**
* **Implementation**:
  * External visible `dist/seed/pi_100m.zst` distributed directly alongside `PiPhone.exe` with embedded fallback.
  * Automatic background seed expansion via `sync_seed_file()` whenever user calculations extend beyond 100M digits.
  * Preserved GitHub 50 MB safety rule by preventing auto-overwrites to Git-tracked root `seed/` in dev mode.

### FB-08: Mouse-Selectable Text Widgets & Clipboard Integration
* **Status**: **Completed (v2.1)**
* **Implementation**:
  * Borderless interactive `tk.Text` (`self.txt_result`) with multi-font tag rendering for context digits (13pt muted) and target matches (24pt bold green).
  * Borderless `ReadonlySelectableEntry` widgets for telemetry, positions, and history cards with native mouse drag highlighting.
  * Full `Ctrl+C` copy and `Ctrl+A` select-all clipboard integration with `_on_key_press` modifier bypass preventing dialpad interference.

---

## 2. Active Backlog Items

### FB-01: "Find Next" Occurrence & Delta Analysis
* **Priority**: High (v2.1)
* **Description**: Allow users to continue searching $\pi$ beyond the first occurrence to find the 2nd, 3rd, or $N$-th occurrence of the same digit sequence.
* **Key Capabilities**:
  * Add a **"Find Next"** button to the results panel after an initial match is found.
  * Maintain an occurrence counter: `"Occurrence #2 at position #3,124,590 (+3,109,698 digits from previous)"`.
  * Display the distance / delta in digits between consecutive occurrences.
  * Plot occurrence interval distribution against the expected geometric distribution ($\mu = 10^L$).

### FB-03: Phone Number Formatting Toggle
* **Priority**: Medium (v2.1)
* **Description**: Provide an optional formatting toggle in the UI to display numbers in familiar telephone formats without affecting the underlying search engine.
* **Key Capabilities**:
  * Formats supported:
    * Standard US: `(NPA) NXX-XXXX` (10 digits) or `NXX-XXXX` (7 digits)
    * International E.164-style grouping
    * Raw unformatted digits (default)
  * Toggle switch in the header bar or settings menu.
  * Search history displays both formatted and unformatted forms.

### FB-04: Export / Share Card Generator
* **Priority**: Medium (v2.2)
* **Description**: Generate a sleek, shareable visual card (PNG / SVG) showcasing where a user's phone number lives in the decimal expansion of $\pi$.
* **Key Capabilities**:
  * Card design features:
    * High-resolution dark-mode aesthetic matching the app theme.
    * Prominently highlighted number within the $\pi$ stream.
    * Exact decimal digit position index and discovery date.
    * QR code linking to an online mathematical verification tool.
  * One-click "Copy Image to Clipboard" or "Save Image As...".

### FB-05: Telephony Audio Sonification & DTMF Dial Tones
* **Priority**: Low (v2.2)
* **Description**: Add authentic Dual-Tone Multi-Frequency (DTMF) dial tones when clicking keypad buttons and audio playback of the matched sequence in $\pi$.
* **Key Capabilities**:
  * Synthesize standard ITU-T DTMF frequencies (e.g., Key `1`: 697 Hz + 1209 Hz) via lightweight `wave` / `winsound` / `numpy` synthesis.
  * Audio preview button to "listen" to the flanking $\pi$ digits converted to pitch frequencies.
  * Mute / volume slider in the header bar.

### FB-07: Statistical & Frequency Analysis Dashboard
* **Priority**: Low (v2.3)
* **Description**: Add a third tab exploring statistical normality and randomness properties of the cached $\pi$ stream.
* **Key Capabilities**:
  * Digit frequency bar chart (uniform distribution check of `0-9`).
  * Chi-Square ($\chi^2$) goodness-of-fit test score.
  * Entropy calculation per 100,000-digit chunk.

---

## 3. Prioritization & Milestone Roadmap

```
v1.0 (MVP)
  +-- CustomTkinter Dark Mode Dialer UI (2-Tab layout)
  +-- 1-10 digit input validation & 11th digit block
  +-- Chudnovsky algorithm engine (mpmath)
  +-- Persistent raw stream (data/pi_digits.txt)
  +-- Timsort & Binary Search index (data/pi_index.json)
  +-- Start-of-Pi "3." context padding

v1.1 - v1.3 (Enhancements & Checkpoint Pausing)
  +-- Checkpoint pause modal with escalating tiers
  +-- Total cached digits telemetry banner
  +-- End-of-chunk lookahead & boundary stitch hardening
  +-- True wall-clock timer loop for time-budget bursts

v2.0 (High-Performance Engine & 100M Seed Pack - Current)
  +-- Bundled 100,000,000-digit compressed seed pack (seed/pi_100m.zst, 40.56 MB)
  +-- Hardware-accelerated C evaluation via gmpy2
  +-- T_min_step Golden Rule (1.25x minimum step duration threshold)
  +-- Cold-start micro-benchmark CPU calibration
  +-- 5-Tier escalating modal schedule with Tier 5 Deep Pi note

v2.1 (Upcoming Follow-up)
  +-- FB-01: "Find Next" Occurrence & Delta Distance
  +-- FB-03: Phone Number Formatting Toggle
```

