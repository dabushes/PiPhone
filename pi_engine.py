"""
PiEngine - High-performance Pi calculation, indexing, and lookup engine.
Implements the Chudnovsky algorithm precision scaling, persistent raw stream caching,
Timsort-maintained binary search indexing, and thread-safe chunk expansion.
"""

from __future__ import annotations

import bisect
import json
import math
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

# Explicit import for PyInstaller bundling of C-extensions
try:
    import gmpy2
except ImportError:
    gmpy2 = None

import mpmath

# Configure mpmath to avoid printing small exponents in scientific notation
mpmath.mp.pretty = True

# Verify and log active mpmath backend ('gmpy' vs 'python')
BACKEND_NAME = getattr(mpmath.libmp, "BACKEND", "python")

DEFAULT_CHUNK_SIZE = 100_000
MAX_QUERY_LENGTH = 10


def get_app_dir() -> Path:
    """
    Returns the root application directory:
    - If frozen by PyInstaller (standalone .exe), returns the directory containing the executable.
    - Otherwise, returns the directory containing this script.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_seed_path() -> Optional[Path]:
    """
    Locates the bundled 100M seed file.
    When running in PyInstaller frozen bundle, sys._MEIPASS is checked first.
    Falls back to sys.executable directory and __file__ directory.
    Supports .zst, .xz, and .gz extensions.
    """
    search_dirs: list[Path] = []
    # 1. PyInstaller frozen unpacked bundle dir (_MEIPASS)
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        meipass_dir = Path(sys._MEIPASS).resolve()
        search_dirs.append(meipass_dir / "seed")
        search_dirs.append(meipass_dir)
    # 2. Executable root directory (next to .exe)
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        search_dirs.append(exe_dir / "seed")
        search_dirs.append(exe_dir)
    # 3. Source script root directory (dev mode)
    script_dir = Path(__file__).resolve().parent
    search_dirs.append(script_dir / "seed")
    search_dirs.append(script_dir)

    for d in search_dirs:
        for fname in ["pi_100m.zst", "pi_100m.xz", "pi_100m.gz", "pi_100m.txt.gz"]:
            candidate = d / fname
            if candidate.exists():
                return candidate
    return None


def decompress_seed_file(seed_path: Path) -> str:
    """
    Decompresses the 100M seed archive into raw ASCII Pi digits string.
    Supports zstandard, lzma, and gzip.
    """
    name = seed_path.name
    with open(seed_path, "rb") as f:
        data = f.read()

    if name.endswith(".zst"):
        try:
            import zstandard as zstd
            dctx = zstd.ZstdDecompressor()
            raw_bytes = dctx.decompress(data)
            return raw_bytes.decode("ascii")
        except ImportError:
            pass

    if name.endswith(".xz"):
        import lzma
        return lzma.decompress(data).decode("ascii")

    if name.endswith(".gz"):
        import gzip
        return gzip.decompress(data).decode("ascii")

    raise RuntimeError(f"Unsupported seed compression format: {name}")


def validate_query(query: str) -> str:
    """
    Validates that a query string contains 1 to 10 numeric digits only.
    Raises ValueError on invalid input.
    """
    if not isinstance(query, str):
        raise ValueError("Query must be a string.")
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("Query cannot be empty.")
    if len(cleaned) > MAX_QUERY_LENGTH:
        raise ValueError(f"Query exceeds maximum length of {MAX_QUERY_LENGTH} digits.")
    if not cleaned.isdigit():
        raise ValueError("Query must contain digits only (0-9).")
    return cleaned


def format_context(
    digits: str,
    pos_1based: int,
    query_len: int,
    include_ellipses: bool = True,
) -> Tuple[str, str, str]:
    """
    Formats the context around a matched digit sequence according to the Start-of-Pi rule:
    - pos_1based is 1 for the first decimal digit (e.g. '1' in 3.1415...)
    - If pos_1based <= 4 (e.g. pos 1, 2, 3, or 4), prepends '3.' and the preceding decimal digits.
      Crucially, the leading ellipsis ('...') is omitted because nothing precedes '3.'.
    - If pos_1based >= 5, extracts the exact 4 preceding digits and prepends '...'.
    - Following context extracts the next 4 decimal digits and appends '...'.

    Args:
        digits: String of contiguous decimal digits of Pi (excluding '3.').
        pos_1based: 1-based decimal starting position.
        query_len: Length of the matched target sequence.
        include_ellipses: Whether to include leading/trailing '...' contextual markers.

    Returns:
        Tuple of (preceding, target, following)
    """
    if pos_1based < 1:
        raise ValueError("pos_1based must be >= 1")

    idx = pos_1based - 1  # 0-based index in the decimal stream
    target = digits[idx : idx + query_len]

    # Preceding context calculation
    if idx < 4:
        # Start-of-Pi edge case (positions 1 through 4):
        # Includes '3.' prefix without leading ellipses since '3.' is the start of Pi.
        preceding = "3." + digits[:idx]
    else:
        raw_pre = digits[idx - 4 : idx]
        preceding = f"...{raw_pre}" if include_ellipses else raw_pre

    # Following context calculation: 4 trailing decimal digits with '...' marker
    raw_following = digits[idx + query_len : idx + query_len + 4]
    following = f"{raw_following}..." if include_ellipses else raw_following

    return preceding, target, following


def compute_pi_digits(total_digits: int, guard_digits: int = 20) -> str:
    """
    Computes `total_digits` decimal digits of Pi (excluding '3.') using mpmath.
    The Chudnovsky algorithm is the underlying arbitrary-precision driver in mpmath.
    Always uses total_digits + 20 dps to eliminate boundary rounding errors,
    converts via mpmath.nstr(mpmath.mp.pi, total_digits + 10), and strictly
    extracts the decimal expansion after '3.'.
    """
    if total_digits <= 0:
        return ""
    # Set decimal precision with 20 guard digits so the last digit never suffers boundary rounding
    mpmath.mp.dps = total_digits + guard_digits
    raw_str = mpmath.nstr(mpmath.mp.pi, total_digits + 10)
    parts = raw_str.split(".")
    if len(parts) > 1:
        decimals = parts[1][:total_digits]
    else:
        decimals = raw_str[2 : 2 + total_digits]
    return decimals


def format_delta_digits(delta: int) -> str:
    """
    Formats an incremental digit count into human-readable compact notation.
    Examples:
        100_000 -> '+100,000 digits'
        1_000_000 -> '+1.0M digits'
        4_200_000 -> '+4.2M digits'
    """
    if delta >= 1_000_000:
        return f"+{delta / 1_000_000:.1f}M digits"
    return f"+{delta:,} digits"


@dataclass
class CheckpointOption:
    label: str
    delta_digits: int = 0
    target_seconds: Optional[float] = None
    mode: str = "fixed_digits"  # "fixed_digits" or "time_budget"


@dataclass
class CheckpointTierConfig:
    tier: int
    t_1m: float
    option1: CheckpointOption
    option2: CheckpointOption
    description: str = ""
    t_min_step: float = 0.0
    note: str = ""


@dataclass
class SearchResult:
    query: str
    position: int  # 1-based index in the decimal expansion
    preceding: str
    target: str
    following: str
    elapsed_seconds: float
    source: str  # "Sorted Index Cache" or "Calculated"
    new_digits_calculated: int = 0


class PiEngine:
    """
    Persistent calculation and search engine for Pi digits.
    """

    def __init__(
        self,
        data_dir: str | Path | None = None,
        default_seed_digits: int = 100_000_000,
        load_seed_pack: Optional[bool] = None,
    ):
        if data_dir is None:
            self.data_dir = get_app_dir() / "data"
        else:
            p = Path(data_dir)
            if p.is_absolute():
                self.data_dir = p
            else:
                self.data_dir = get_app_dir() / p
        self.data_dir.mkdir(parents=True, exist_ok=True)

        if load_seed_pack is None:
            # Load seed pack by default in production or whenever data_dir is standard data/
            if data_dir is None or str(data_dir).rstrip("/\\") in ("data", str(get_app_dir() / "data")):
                self._should_load_seed = True
            else:
                self._should_load_seed = (default_seed_digits >= 100_000_000)
        else:
            self._should_load_seed = load_seed_pack

        self.digits_file = self.data_dir / "pi_digits.txt"
        self.index_file = self.data_dir / "pi_index.json"
        self.history_file = self.data_dir / "search_history.json"

        self._lock = threading.Lock()
        self.digits: str = ""
        # Sorted list of (query, position_1based) for fast binary search
        self.query_cache: list[tuple[str, int]] = []
        # Sorted list of (prefix_4digit, first_occurrence_1based)
        self.prefix_index: list[tuple[str, int]] = []

        # Empirical calculation benchmark tracking
        self.has_calibrated_benchmark: bool = False
        if BACKEND_NAME == "gmpy":
            self.last_calc_digits: int = 100_000
            self.last_calc_time: float = 0.05
        else:
            self.last_calc_digits: int = 100_000
            self.last_calc_time: float = 0.35

        self._load_state(default_seed_digits)

    def calibrate_benchmark(self, force: bool = False) -> None:
        """
        Runs a quick micro-benchmark (calculating 500,000 digits in memory)
        to calibrate N_last and T_last to the exact host CPU performance,
        without modifying or overwriting self.digits or disk files.
        """
        if self.has_calibrated_benchmark and not force:
            return

        bench_digits = 500_000
        t_start = time.perf_counter()
        _ = compute_pi_digits(bench_digits)
        elapsed = time.perf_counter() - t_start

        self.last_calc_digits = bench_digits
        self.last_calc_time = max(0.001, elapsed)
        self.has_calibrated_benchmark = True

    def _load_state(self, default_seed_digits: int) -> None:
        """Loads cached digits and sorted indices from disk, extracts bundled seed, or seeds initial digits."""
        # Check if digits_file exists and is full 100M
        file_needs_seed = True
        if self.digits_file.exists():
            try:
                file_size = self.digits_file.stat().st_size
                if file_size >= 100_000_000:
                    file_needs_seed = False
                    with open(self.digits_file, "r", encoding="ascii") as f:
                        self.digits = f.read().strip()
                else:
                    # File exists but is smaller than 100M (e.g. outdated/partial cache)
                    file_needs_seed = True
            except Exception:
                file_needs_seed = True

        # Extract bundled 100M seed if file is missing or smaller than 100M
        if file_needs_seed and self._should_load_seed:
            seed_path = get_seed_path()
            if seed_path is not None:
                try:
                    seed_digits = decompress_seed_file(seed_path)
                    if len(seed_digits) >= 100_000_000:
                        self.digits = seed_digits
                        with open(self.digits_file, "w", encoding="ascii") as f:
                            f.write(self.digits)
                        file_needs_seed = False
                except Exception:
                    pass

        # If file existed and seed was not loaded (or disabled), load whatever was in file
        if file_needs_seed and not self.digits and self.digits_file.exists():
            try:
                with open(self.digits_file, "r", encoding="ascii") as f:
                    self.digits = f.read().strip()
            except Exception:
                pass

        # Seed initial digits if empty and requested (fallback)
        if len(self.digits) < default_seed_digits and default_seed_digits > 0:
            initial = compute_pi_digits(default_seed_digits)
            self.digits = initial
            with open(self.digits_file, "w", encoding="ascii") as f:
                f.write(self.digits)

        # Load persisted index
        if self.index_file.exists():
            try:
                with open(self.index_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.query_cache = [tuple(item) for item in data.get("query_cache", [])]
                    self.prefix_index = [tuple(item) for item in data.get("prefix_index", [])]
            except Exception:
                self.query_cache = []
                self.prefix_index = []

        # Ensure query cache is strictly sorted using Timsort
        self._sort_indices()

    def _sort_indices(self) -> None:
        """Sorts query cache using Python's native Timsort."""
        self.query_cache.sort(key=lambda x: x[0])
        self.prefix_index.sort(key=lambda x: x[0])

    def _save_index(self) -> None:
        """Saves sorted query cache to disk."""
        data = {
            "query_cache": self.query_cache,
            "prefix_index": self.prefix_index,
        }
        with open(self.index_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    @property
    def pi_digits(self) -> str:
        """Alias for self.digits ensuring compatibility with pi_digits references."""
        return self.digits

    @pi_digits.setter
    def pi_digits(self, value: str) -> None:
        self.digits = value

    def lookup_cache(self, query: str) -> Optional[int]:
        """
        Binary search against the sorted query cache. Returns 1-based position if found.
        Time Complexity: O(log n)
        """
        if not self.query_cache:
            return None
        keys = [item[0] for item in self.query_cache]
        idx = bisect.bisect_left(keys, query)
        if idx < len(self.query_cache) and self.query_cache[idx][0] == query:
            return self.query_cache[idx][1]
        return None

    def lookup_prefix(self, prefix: str) -> Optional[int]:
        """
        Returns first occurrence 1-based position of 4-digit prefix.
        Fast C-level find guarantees 100% determinism without skipped ranges.
        """
        if len(prefix) != 4:
            return None
        idx = self.digits.find(prefix)
        if idx != -1:
            return idx + 1
        return None

    def record_query_match(self, query: str, position_1based: int) -> None:
        """Records a resolved query to the sorted index using Timsort and persists it."""
        with self._lock:
            # Check if already in cache
            if self.lookup_cache(query) is None:
                self.query_cache.append((query, position_1based))
                # Native Timsort
                self.query_cache.sort(key=lambda x: x[0])
                self._save_index()

    def search_cached_digits(self, query: str) -> Optional[int]:
        """Scans current in-memory digits for query from index 0 across entire string. Returns 1-based position or None."""
        idx = self.digits.find(query)
        if idx != -1:
            return idx + 1
        return None

    def get_total_cached_digits(self) -> int:
        """
        Returns the total count of decimal digits of Pi currently calculated
        and stored in memory/disk.

        Returns:
            int: Number of decimal digits cached.
        """
        with self._lock:
            return len(self.digits)

    def estimate_chunk_time(self, current_total: int, chunk_delta: int) -> float:
        """
        Estimates the wall-clock compute duration (in seconds) to expand Pi from
        current_total to (current_total + chunk_delta) digits using the empirical
        super-linear scaling law:
            T_est = T_last * ((current_total + chunk_delta) / N_last) ** 1.35

        Args:
            current_total: Number of decimal digits currently calculated.
            chunk_delta: Incremental digits to be generated in the next step.

        Returns:
            float: Estimated execution time in seconds.
        """
        if self.last_calc_digits <= 0:
            return 0.05
        target = current_total + chunk_delta
        ratio = max(0.001, target / self.last_calc_digits)
        return float(self.last_calc_time * (ratio ** 1.65))

    @staticmethod
    def format_estimated_time(seconds: float) -> str:
        """
        Formats an estimated duration in seconds into a clean bracketed label.
        Examples: '[< 0.1s]', '[~1.4s]', '[~42s]'.

        Args:
            seconds: Estimated time in seconds.

        Returns:
            str: Human-readable bracketed label.
        """
        if seconds < 0.1:
            return "[< 0.1s]"
        elif seconds < 10.0:
            return f"[~{seconds:.1f}s]"
        elif seconds < 60.0:
            return f"[~{round(seconds)}s]"
        else:
            mins = seconds / 60.0
            return f"[~{mins:.1f}m]"

    def should_show_1m_step(self, current_total: int) -> bool:
        """
        Evaluates the 60-second time-ceiling rule for +1,000,000 digits.

        Args:
            current_total: Current count of cached decimal digits.

        Returns:
            bool: True if estimated compute time is <= 60.0s, False if > 60.0s.
        """
        est_1m = self.estimate_chunk_time(current_total, 1_000_000)
        return est_1m <= 60.0

    def estimate_digits_for_time(
        self,
        current_total: int,
        target_seconds: float,
        min_floor: int = 100_000,
        max_cap: Optional[int] = 25_000_000,
    ) -> int:
        """
        Calculates the number of incremental digits Delta N that can be computed
        within a target time budget (target_seconds) based on the inverse scaling law:
            N_target = int(N_last * (target_seconds / T_last) ** (1 / 1.65))
            Delta N = max(min_floor, round((N_target - current_total) / 100_000) * 100_000)
        Enforces min_floor (default 100,000) and max_cap (default 25,000,000).

        Args:
            current_total: Number of decimal digits currently calculated.
            target_seconds: Desired wall-clock time budget in seconds.
            min_floor: Minimum incremental digits floor.
            max_cap: Maximum incremental digits ceiling (e.g. 25M).

        Returns:
            int: Incremental digits to calculate, rounded to the nearest 100,000.
        """
        if target_seconds <= 0:
            return min_floor

        n_last = max(100_000, self.last_calc_digits)
        t_last = max(0.001, self.last_calc_time)

        ratio = target_seconds / t_last
        n_target = int(n_last * (ratio ** (1.0 / 1.65)))
        raw_delta = n_target - current_total

        # Round to nearest 100,000 with minimum floor
        rounded_delta = round(raw_delta / 100_000) * 100_000
        delta = max(min_floor, rounded_delta)

        # Cap delta to prevent runaway single-step calculations
        if max_cap is not None:
            delta = min(delta, max_cap)

        return delta

    def get_checkpoint_tier_config(self, current_total: int) -> CheckpointTierConfig:
        """
        Evaluates the 5-tier escalating checkpoint schedule based on T_min_step
        (minimum step = +2,000,000 digits) and T_1M, enforcing the Golden Rule:
        A time-budget button may NEVER offer a duration shorter than 1.25 * T_min_step.

        - Tier 1 (t_min_step < 15.0s):
            Option 1: Continue (+1,000,000 Digits) [~X.Xs]
            Option 2: Search for 30 Seconds
        - Tier 2 (15.0s <= t_min_step < 24.0s):
            Option 1: Continue (+1,000,000 Digits) [~XXs]
            Option 2: Search for 60 Seconds
        - Tier 3 (24.0s <= t_min_step < 48.0s):
            Option 1: Search for 1 Minute
            Option 2: Search for 2 Minutes
        - Tier 4 (48.0s <= t_min_step <= 90.0s):
            Option 1: Search for 2 Minutes
            Option 2: Search for 5 Minutes
        - Tier 5 (t_min_step > 90.0s — Deep Pi / 100M+ Mode):
            Option 1: Calculate Next Block (~{ceil(t_min_step / 60)} Mins)
            Option 2: Search for {ceil(t_min_step * 2.2 / 60)} Minutes
            Informative Note: "At {current_total:,} digits, computing the next block of Pi requires ~{t_min_step / 60:.1f} minutes."

        Args:
            current_total: Number of decimal digits currently cached.

        Returns:
            CheckpointTierConfig with configured options and metadata.
        """
        # Ensure CPU benchmark is calibrated if dealing with deep caches
        if current_total >= 10_000_000 and not self.has_calibrated_benchmark:
            self.calibrate_benchmark()

        t_min_step = self.estimate_chunk_time(current_total, 2_000_000)
        t_1m = self.estimate_chunk_time(current_total, 1_000_000)
        fmt_1m = self.format_estimated_time(t_1m)

        if t_min_step < 15.0:
            d_30s = self.estimate_digits_for_time(current_total, 30.0)
            opt1 = CheckpointOption(
                label=f"Continue (+1,000,000 Digits) {fmt_1m}",
                delta_digits=1_000_000,
                target_seconds=t_1m,
                mode="fixed_digits",
            )
            opt2 = CheckpointOption(
                label="Search for 30 Seconds",
                delta_digits=d_30s,
                target_seconds=30.0,
                mode="time_budget",
            )
            return CheckpointTierConfig(
                tier=1,
                t_1m=t_1m,
                t_min_step=t_min_step,
                option1=opt1,
                option2=opt2,
                description="Fast calculation (< 15s per min step)",
            )
        elif t_min_step < 24.0:
            d_60s = self.estimate_digits_for_time(current_total, 60.0)
            opt1 = CheckpointOption(
                label=f"Continue (+1,000,000 Digits) {fmt_1m}",
                delta_digits=1_000_000,
                target_seconds=t_1m,
                mode="fixed_digits",
            )
            opt2 = CheckpointOption(
                label="Search for 60 Seconds",
                delta_digits=d_60s,
                target_seconds=60.0,
                mode="time_budget",
            )
            return CheckpointTierConfig(
                tier=2,
                t_1m=t_1m,
                t_min_step=t_min_step,
                option1=opt1,
                option2=opt2,
                description="Moderate calculation (15s - 24s per min step)",
            )
        elif t_min_step < 48.0:
            d_60s = self.estimate_digits_for_time(current_total, 60.0)
            d_120s = self.estimate_digits_for_time(current_total, 120.0)
            opt1 = CheckpointOption(
                label="Search for 1 Minute",
                delta_digits=d_60s,
                target_seconds=60.0,
                mode="time_budget",
            )
            opt2 = CheckpointOption(
                label="Search for 2 Minutes",
                delta_digits=d_120s,
                target_seconds=120.0,
                mode="time_budget",
            )
            return CheckpointTierConfig(
                tier=3,
                t_1m=t_1m,
                t_min_step=t_min_step,
                option1=opt1,
                option2=opt2,
                description="Extended calculation (24s - 48s per min step; +1M retired)",
            )
        elif t_min_step <= 90.0:
            d_120s = self.estimate_digits_for_time(current_total, 120.0)
            d_300s = self.estimate_digits_for_time(current_total, 300.0)
            opt1 = CheckpointOption(
                label="Search for 2 Minutes",
                delta_digits=d_120s,
                target_seconds=120.0,
                mode="time_budget",
            )
            opt2 = CheckpointOption(
                label="Search for 5 Minutes",
                delta_digits=d_300s,
                target_seconds=300.0,
                mode="time_budget",
            )
            return CheckpointTierConfig(
                tier=4,
                t_1m=t_1m,
                t_min_step=t_min_step,
                option1=opt1,
                option2=opt2,
                description="Heavy calculation (48s - 90s per min step)",
            )
        else:
            mins_step = math.ceil(t_min_step / 60.0)
            mins_search = math.ceil((t_min_step * 2.2) / 60.0)
            note = (
                f"At {current_total:,} digits, computing the next block of Pi "
                f"requires ~{t_min_step / 60.0:.1f} minutes."
            )
            opt1 = CheckpointOption(
                label=f"Calculate Next Block (~{mins_step} Mins)",
                delta_digits=2_000_000,
                target_seconds=max(1.25 * t_min_step, 180.0),
                mode="fixed_digits",
            )
            opt2 = CheckpointOption(
                label=f"Search for {mins_search} Minutes",
                delta_digits=4_000_000,
                target_seconds=mins_search * 60.0,
                mode="time_budget",
            )
            return CheckpointTierConfig(
                tier=5,
                t_1m=t_1m,
                t_min_step=t_min_step,
                option1=opt1,
                option2=opt2,
                description="Deep Pi calculation (> 90s per min step; 100M+ mode)",
                note=note,
            )

    def search(
        self,
        query: str,
        chunk_size: Optional[int] = None,
        cancel_event: Optional[threading.Event] = None,
        progress_callback: Optional[Callable[[int, float], None]] = None,
        checkpoint_callback: Optional[Callable[..., str | int | None]] = None,
    ) -> Optional[SearchResult]:
        """
        Executes the search strategy:
        1. Validates query (1 to 10 digits).
        2. Binary search in sorted query cache (O(log n)).
        3. Scan existing cached digits.
        4. If not found, dynamically expands Pi. If chunk_size is unspecified, performs an
           initial burst up to 1,000,000 digits (or Button 1's step size if >= 1,000,000).
           Searches each chunk with overlap = len(query) - 1 to handle boundary-straddling matches.
        5. If sequence is not found in a chunk and checkpoint_callback is provided, pauses and
           calls checkpoint_callback(new_digits_total, total_cached_digits, tier_config):
           - If callback returns 'stop' or None, cleanly halts and returns None.
           - If callback returns an int > 0, updates current_step.
        6. Supports cooperative cancellation via cancel_event.

        Args:
            query: User-entered digit string (1-10 digits).
            chunk_size: Optional chunk step size for expansion. If None, defaults to initial 1M burst.
            cancel_event: Optional threading event to signal cancellation.
            progress_callback: Optional callback(digits_searched, elapsed_seconds).
            checkpoint_callback: Optional callback returning new step size (int) or 'stop' / None to halt.

        Returns:
            SearchResult if found, or None if cancelled or stopped at checkpoint.
        """
        clean_query = validate_query(query)
        start_time = time.perf_counter()

        # Step 1: Binary search query cache (O(log n))
        cached_pos = self.lookup_cache(clean_query)
        if cached_pos is not None:
            # Verify we have enough trailing digits in self.digits for following context
            needed_len = cached_pos - 1 + len(clean_query) + 4
            if len(self.digits) < needed_len:
                self._expand_to_length(needed_len)

            elapsed = time.perf_counter() - start_time
            preceding, target, following = format_context(self.digits, cached_pos, len(clean_query))
            return SearchResult(
                query=clean_query,
                position=cached_pos,
                preceding=preceding,
                target=target,
                following=following,
                elapsed_seconds=elapsed,
                source="Sorted Index Cache",
                new_digits_calculated=0,
            )

        # Step 2: Scan existing cached digits from index 0 across entire string
        pos_in_cache = self.digits.find(clean_query)
        if pos_in_cache != -1:
            global_pos = pos_in_cache + 1
            needed_len = pos_in_cache + len(clean_query) + 4
            if len(self.digits) < needed_len:
                self._expand_to_length(needed_len)

            self.record_query_match(clean_query, global_pos)
            elapsed = time.perf_counter() - start_time
            preceding, target, following = format_context(self.digits, global_pos, len(clean_query))
            return SearchResult(
                query=clean_query,
                position=global_pos,
                preceding=preceding,
                target=target,
                following=following,
                elapsed_seconds=elapsed,
                source="Sorted Index Cache",
                new_digits_calculated=0,
            )

        # Step 3: Dynamic chunk expansion with checkpoint pausing & wall-clock budget loops
        q_len = len(clean_query)
        new_digits_total = 0

        current_len = len(self.digits)
        if chunk_size is not None:
            pending_action: Any = chunk_size
        else:
            if current_len < 1_000_000:
                pending_action = 1_000_000 - current_len
            else:
                tier_cfg = self.get_checkpoint_tier_config(current_len)
                pending_action = tier_cfg.option1

        def report_progress(digits_searched: int, elapsed: float, status_msg: Optional[str] = None):
            if progress_callback:
                try:
                    progress_callback(digits_searched, elapsed, status_msg)
                except TypeError:
                    progress_callback(digits_searched, elapsed)

        while True:
            if cancel_event is not None and cancel_event.is_set():
                return None

            # Determine whether pending_action is a time-budget burst or a fixed digit step
            is_time_budget = False
            target_seconds = 0.0

            if isinstance(pending_action, CheckpointOption):
                if pending_action.mode == "time_budget" and pending_action.target_seconds:
                    is_time_budget = True
                    target_seconds = float(pending_action.target_seconds)
                else:
                    step_delta = pending_action.delta_digits if pending_action.delta_digits > 0 else 1_000_000
            elif isinstance(pending_action, dict) and pending_action.get("mode") == "time_budget":
                is_time_budget = True
                target_seconds = float(pending_action.get("seconds", 30.0))
            elif isinstance(pending_action, float):
                is_time_budget = True
                target_seconds = pending_action
            elif isinstance(pending_action, int) and pending_action > 0:
                step_delta = pending_action
            else:
                step_delta = 1_000_000

            if is_time_budget:
                start_burst = time.perf_counter()
                deadline = start_burst + target_seconds
                budget_label = int(target_seconds) if target_seconds.is_integer() else f"{target_seconds:.1f}"

                while time.perf_counter() < deadline:
                    if cancel_event is not None and cancel_event.is_set():
                        return None

                    now = time.perf_counter()
                    elapsed_burst = now - start_burst
                    remaining_time = max(0.0, deadline - now)

                    # Exit burst if remaining time is insufficient for a safe multi-million chunk
                    exit_thresh = 2.0 if target_seconds > 3.0 else min(0.2, target_seconds * 0.15)
                    if remaining_time < exit_thresh and elapsed_burst > 0.05:
                        break

                    min_floor = 2_000_000 if target_seconds > 3.0 else 50_000
                    delta_n = self.estimate_digits_for_time(
                        len(self.digits),
                        remaining_time,
                        min_floor=min_floor,
                        max_cap=25_000_000,
                    )

                    old_total = len(self.digits)
                    target_len = old_total + delta_n

                    t_calc_start = time.perf_counter()
                    new_digits = compute_pi_digits(target_len)
                    calc_duration = time.perf_counter() - t_calc_start

                    self.last_calc_digits = target_len
                    self.last_calc_time = max(0.001, calc_duration)
                    new_digits_total += (len(new_digits) - old_total)

                    with self._lock:
                        self.digits = new_digits
                        with open(self.digits_file, "w", encoding="ascii") as f:
                            f.write(self.digits)

                    cur_elapsed = time.perf_counter() - start_burst
                    report_progress(
                        len(self.digits),
                        time.perf_counter() - start_time,
                        f"Running {budget_label}s Burst... [{cur_elapsed:.1f}s / {budget_label}s] — {len(self.digits):,} digits searched",
                    )

                    # Substring match search across newly expanded digits + boundary stitch
                    scan_start = max(0, old_total - q_len + 1)
                    idx = self.digits.find(clean_query, scan_start)
                    if idx != -1:
                        global_pos = idx + 1
                        needed_len = idx + q_len + 4
                        if len(self.digits) < needed_len:
                            self._expand_to_length(needed_len)

                        self.record_query_match(clean_query, global_pos)
                        elapsed = time.perf_counter() - start_time
                        preceding, target, following = format_context(self.digits, global_pos, q_len)
                        return SearchResult(
                            query=clean_query,
                            position=global_pos,
                            preceding=preceding,
                            target=target,
                            following=following,
                            elapsed_seconds=elapsed,
                            source=f"Calculated {new_digits_total:,} new digits",
                            new_digits_calculated=new_digits_total,
                        )

            else:
                # Fixed digit chunk expansion
                old_total = len(self.digits)
                target_len = old_total + step_delta

                t_calc_start = time.perf_counter()
                new_digits = compute_pi_digits(target_len)
                calc_duration = time.perf_counter() - t_calc_start

                self.last_calc_digits = target_len
                self.last_calc_time = max(0.001, calc_duration)
                new_digits_total += (len(new_digits) - old_total)

                with self._lock:
                    self.digits = new_digits
                    with open(self.digits_file, "w", encoding="ascii") as f:
                        f.write(self.digits)

                report_progress(len(self.digits), time.perf_counter() - start_time)

                scan_start = max(0, old_total - q_len + 1)
                idx = self.digits.find(clean_query, scan_start)
                if idx != -1:
                    global_pos = idx + 1
                    needed_len = idx + q_len + 4
                    if len(self.digits) < needed_len:
                        self._expand_to_length(needed_len)

                    self.record_query_match(clean_query, global_pos)
                    elapsed = time.perf_counter() - start_time
                    preceding, target, following = format_context(self.digits, global_pos, q_len)
                    return SearchResult(
                        query=clean_query,
                        position=global_pos,
                        preceding=preceding,
                        target=target,
                        following=following,
                        elapsed_seconds=elapsed,
                        source=f"Calculated {new_digits_total:,} new digits",
                        new_digits_calculated=new_digits_total,
                    )

            # Checkpoint Pause: Sequence not found yet in this chunk or wall-clock burst
            if checkpoint_callback is not None:
                tier_cfg = self.get_checkpoint_tier_config(len(self.digits))
                try:
                    import inspect
                    sig = inspect.signature(checkpoint_callback)
                    params = list(sig.parameters.values())
                    accepts_3 = len(params) >= 3 or any(
                        p.kind == inspect.Parameter.VAR_POSITIONAL for p in params
                    )
                except Exception:
                    accepts_3 = False

                if accepts_3:
                    action = checkpoint_callback(new_digits_total, len(self.digits), tier_cfg)
                else:
                    action = checkpoint_callback(new_digits_total, len(self.digits))

                if action == "stop" or action is None:
                    return None
                else:
                    pending_action = action
            else:
                # If no callback provided, default to 1,000,000 digits continuation
                pending_action = 1_000_000

    def _expand_to_length(self, target_len: int) -> None:
        """Expands self.digits to at least target_len digits and saves to disk."""
        if len(self.digits) < target_len:
            with self._lock:
                t_calc_start = time.perf_counter()
                self.digits = compute_pi_digits(target_len)
                calc_duration = time.perf_counter() - t_calc_start
                self.last_calc_digits = target_len
                self.last_calc_time = max(0.001, calc_duration)
                with open(self.digits_file, "w", encoding="ascii") as f:
                    f.write(self.digits)

    def log_history(self, result: SearchResult) -> None:
        """Appends a search result to the search history JSON log."""
        history = []
        if self.history_file.exists():
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = []
        
        record = asdict(result)
        record["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
        history.append(record)
        
        with open(self.history_file, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)

