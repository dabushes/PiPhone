"""
PiEngine - High-performance Pi calculation, indexing, and lookup engine.
Implements the Chudnovsky algorithm precision scaling, persistent raw stream caching,
Timsort-maintained binary search indexing, and thread-safe chunk expansion.
"""

from __future__ import annotations

import bisect
import json
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

import mpmath

# Configure mpmath to avoid printing small exponents in scientific notation
mpmath.mp.pretty = True

DEFAULT_CHUNK_SIZE = 10_000
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


def compute_pi_digits(num_digits: int, guard_digits: int = 15) -> str:
    """
    Computes `num_digits` decimal digits of Pi (excluding '3.') using mpmath.
    The Chudnovsky algorithm is the underlying arbitrary-precision driver in mpmath.
    """
    if num_digits <= 0:
        return ""
    # Set decimal precision with guard digits to avoid boundary rounding error
    mpmath.mp.dps = num_digits + guard_digits
    pi_str = mpmath.nstr(mpmath.pi, n=num_digits + guard_digits)
    # pi_str starts with "3." followed by the decimal expansion
    decimal_digits = pi_str[2 : 2 + num_digits]
    return decimal_digits


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

    def __init__(self, data_dir: str | Path | None = None, default_seed_digits: int = 10_000):
        if data_dir is None:
            self.data_dir = get_app_dir() / "data"
        else:
            p = Path(data_dir)
            if p.is_absolute():
                self.data_dir = p
            else:
                self.data_dir = get_app_dir() / p
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.digits_file = self.data_dir / "pi_digits.txt"
        self.index_file = self.data_dir / "pi_index.json"
        self.history_file = self.data_dir / "search_history.json"

        self._lock = threading.Lock()
        self.digits: str = ""
        # Sorted list of (query, position_1based) for fast binary search
        self.query_cache: list[tuple[str, int]] = []
        # Sorted list of (prefix_4digit, first_occurrence_1based)
        self.prefix_index: list[tuple[str, int]] = []

        self._load_state(default_seed_digits)

    def _load_state(self, default_seed_digits: int) -> None:
        """Loads cached digits and sorted indices from disk, or seeds initial digits."""
        if self.digits_file.exists():
            with open(self.digits_file, "r", encoding="ascii") as f:
                self.digits = f.read().strip()
        
        # Seed initial digits if empty and requested
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

        # Ensure indices are strictly sorted using Timsort
        self._sort_indices()

        # Build prefix index from current digits if prefix_index is empty
        if not self.prefix_index and len(self.digits) >= 4:
            self._rebuild_prefix_index()

    def _sort_indices(self) -> None:
        """Sorts cache and prefix indices using Python's native Timsort."""
        self.query_cache.sort(key=lambda x: x[0])
        self.prefix_index.sort(key=lambda x: x[0])

    def _rebuild_prefix_index(self) -> None:
        """Builds a 4-digit prefix index of first occurrences in self.digits."""
        seen: dict[str, int] = {}
        for i in range(len(self.digits) - 3):
            prefix = self.digits[i : i + 4]
            if prefix not in seen:
                seen[prefix] = i + 1  # 1-based position
        
        self.prefix_index = [(k, v) for k, v in seen.items()]
        self._sort_indices()
        self._save_index()

    def _save_index(self) -> None:
        """Saves sorted indices to disk."""
        data = {
            "query_cache": self.query_cache,
            "prefix_index": self.prefix_index,
        }
        with open(self.index_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

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
        Binary search against the sorted 4-digit prefix index.
        Returns first occurrence 1-based position if found.
        """
        if not self.prefix_index or len(prefix) != 4:
            return None
        keys = [item[0] for item in self.prefix_index]
        idx = bisect.bisect_left(keys, prefix)
        if idx < len(self.prefix_index) and self.prefix_index[idx][0] == prefix:
            return self.prefix_index[idx][1]
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
        """Scans current in-memory digits for query. Returns 1-based position or None."""
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

    def search(
        self,
        query: str,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        cancel_event: Optional[threading.Event] = None,
        progress_callback: Optional[Callable[[int, float], None]] = None,
        checkpoint_callback: Optional[Callable[[int, int], str | int | None]] = None,
    ) -> Optional[SearchResult]:
        """
        Executes the search strategy:
        1. Validates query (1 to 10 digits).
        2. Binary search in sorted query cache (O(log n)).
        3. Scan existing cached digits.
        4. If not found, iteratively expands Pi by current_step (default chunk_size=10,000),
           searching each chunk with overlap = len(query) - 1 to handle boundary-straddling matches.
        5. If sequence is not found in a chunk and checkpoint_callback is provided, pauses and
           calls checkpoint_callback(new_digits_total, total_cached_digits):
           - If callback returns 'stop' or None, cleanly halts and returns None.
           - If callback returns an int > 0 (e.g. 10_000 or 100_000), updates current_step.
        6. Supports cooperative cancellation via cancel_event.

        Args:
            query: User-entered digit string (1-10 digits).
            chunk_size: Initial chunk step size for expansion (default 10,000).
            cancel_event: Optional threading event to signal cancellation.
            progress_callback: Optional callback(digits_searched, elapsed_seconds).
            checkpoint_callback: Optional callback(new_digits_calculated, total_cached) returning
                                 new step size (int) or 'stop' / None to halt.

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

        # Step 2: Scan existing cached digits
        pos_in_cache = self.search_cached_digits(clean_query)
        if pos_in_cache is not None:
            needed_len = pos_in_cache - 1 + len(clean_query) + 4
            if len(self.digits) < needed_len:
                self._expand_to_length(needed_len)

            self.record_query_match(clean_query, pos_in_cache)
            elapsed = time.perf_counter() - start_time
            preceding, target, following = format_context(self.digits, pos_in_cache, len(clean_query))
            return SearchResult(
                query=clean_query,
                position=pos_in_cache,
                preceding=preceding,
                target=target,
                following=following,
                elapsed_seconds=elapsed,
                source="Sorted Index Cache",
                new_digits_calculated=0,
            )

        # Step 3: Dynamic chunk expansion with checkpoint pausing
        q_len = len(clean_query)
        overlap_len = q_len - 1
        new_digits_total = 0
        current_step = chunk_size

        while True:
            if cancel_event is not None and cancel_event.is_set():
                return None

            current_len = len(self.digits)
            target_len = current_len + current_step

            # Generate new chunk using Chudnovsky algorithm
            new_digits = compute_pi_digits(target_len)
            new_chunk = new_digits[current_len:]
            new_digits_total += len(new_chunk)

            # Search in window: tail of old digits (overlap) + new_chunk
            overlap_start = max(0, current_len - overlap_len)
            overlap_slice = self.digits[overlap_start:]
            search_window = overlap_slice + new_chunk

            window_idx = search_window.find(clean_query)

            # Update master digits and persist to disk
            with self._lock:
                self.digits = new_digits
                with open(self.digits_file, "w", encoding="ascii") as f:
                    f.write(self.digits)

            if progress_callback:
                progress_callback(len(self.digits), time.perf_counter() - start_time)

            if window_idx != -1:
                # Match found! Calculate global 1-based position
                global_pos = (overlap_start + window_idx) + 1

                # Ensure we have 4 following digits for context
                needed_len = global_pos - 1 + q_len + 4
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

            # Checkpoint Pause: Sequence not found yet in this chunk
            if checkpoint_callback is not None:
                # Callback triggers interactive modal or test handler
                action = checkpoint_callback(new_digits_total, len(self.digits))
                if action == "stop" or action is None:
                    return None
                elif isinstance(action, int) and action > 0:
                    current_step = action

    def _expand_to_length(self, target_len: int) -> None:
        """Expands self.digits to at least target_len digits and saves to disk."""
        if len(self.digits) < target_len:
            with self._lock:
                self.digits = compute_pi_digits(target_len)
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

