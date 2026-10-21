"""
PiEngine - High-performance Pi calculation, indexing, and lookup engine.
Implements the Chudnovsky algorithm precision scaling, persistent raw stream caching,
Timsort-maintained binary search indexing, and thread-safe chunk expansion.
"""

from __future__ import annotations

import bisect
import gc
import json
import math
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from pi_binary_index import (
    BinaryIndexMode,
    PiBinaryIndex,
    format_latency,
)

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


def get_seed_path(seed_dir: Optional[Path | str] = None) -> Optional[Path]:
    """
    Locates the 100M seed file.
    Search priority:
    1. Explicit custom seed_dir if provided.
    2. When running in PyInstaller frozen bundle:
       a. External visible seed directory next to .exe: Path(sys.executable).parent / "seed"
       b. Next to executable: Path(sys.executable).parent
       c. Embedded fallback: sys._MEIPASS / "seed" and sys._MEIPASS
    3. In dev mode:
       a. Project root seed/ directory: __file__.parent / "seed"
       b. Project root directory: __file__.parent
    Supports .zst, .xz, and .gz extensions.
    """
    search_dirs: list[Path] = []

    # 0. Custom seed directory if specified
    if seed_dir is not None:
        s_p = Path(seed_dir).resolve()
        search_dirs.append(s_p)
        search_dirs.append(s_p / "seed")

    # 1. Frozen executable external visible directory
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        search_dirs.append(exe_dir / "seed")
        search_dirs.append(exe_dir)
        # Embedded fallback in temporary unpacked dir (_MEIPASS)
        if hasattr(sys, "_MEIPASS"):
            meipass_dir = Path(sys._MEIPASS).resolve()
            search_dirs.append(meipass_dir / "seed")
            search_dirs.append(meipass_dir)
    else:
        # 2. Source script root directory (dev mode)
        script_dir = Path(__file__).resolve().parent
        search_dirs.append(script_dir / "seed")
        search_dirs.append(script_dir)

    for d in search_dirs:
        for fname in ["pi_100m.zst", "pi_100m.xz", "pi_100m.gz", "pi_100m.txt.gz"]:
            candidate = d / fname
            if candidate.exists():
                return candidate
    return None


def compress_seed_file(digits: str, output_path: Path) -> None:
    """
    Compresses digits string to output_path using zstandard, lzma, or gzip
    based on file extension. Writes atomically via temporary file.
    """
    import uuid

    raw_bytes = digits.encode("ascii")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.parent / f"{output_path.name}.{uuid.uuid4().hex}.tmp"

    try:
        if output_path.name.endswith(".zst"):
            try:
                import zstandard as zstd
                # Level 3 provides fast incremental compression for expanded streams
                cctx = zstd.ZstdCompressor(level=3)
                compressed = cctx.compress(raw_bytes)
                with open(temp_path, "wb") as f:
                    f.write(compressed)
                temp_path.replace(output_path)
                return
            except ImportError:
                pass

        if output_path.name.endswith(".xz"):
            import lzma
            compressed = lzma.compress(raw_bytes, preset=1)
            with open(temp_path, "wb") as f:
                f.write(compressed)
            temp_path.replace(output_path)
            return

        import gzip
        compressed = gzip.compress(raw_bytes, compresslevel=3)
        with open(temp_path, "wb") as f:
            f.write(compressed)
        temp_path.replace(output_path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass


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
    if not (cleaned.isascii() and cleaned.isdigit()):
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


def _compute_pi_worker(target_len: int, temp_file_path: str, result_queue: Any, old_len: int = 0) -> None:
    """Worker process entry point for out-of-process Pi digit calculation."""
    try:
        digits = compute_pi_digits(target_len)
        slice_to_write = digits[old_len:] if old_len > 0 else digits
        with open(temp_file_path, "w", encoding="ascii") as f:
            f.write(slice_to_write)
        result_queue.put({"success": True, "len": len(slice_to_write), "full_len": len(digits)})
    except Exception as e:
        result_queue.put({"success": False, "error": str(e)})


def compute_pi_digits_cancellable(
    target_len: int,
    old_len: int = 0,
    cancel_event: Optional[threading.Event] = None,
    poll_interval: float = 0.05,
    temp_dir: Optional[Path] = None,
) -> Optional[str]:
    """
    Computes target_len decimal digits of Pi inside a dedicated child process.
    If old_len > 0, returns only the newly calculated slice decimals[old_len:target_len]
    (~2MB) to eliminate massive multi-hundred megabyte IPC and memory bottlenecks.
    Allows instantaneous cancellation (< 0.05s) via process.terminate() if
    cancel_event is set during computation.
    """
    if target_len <= 0 or target_len <= old_len:
        return ""

    # If no cancel_event is provided or target_len is small (< 100k), calculate directly in process
    if cancel_event is None or target_len <= 100_000:
        if cancel_event is not None and cancel_event.is_set():
            return None
        res = compute_pi_digits(target_len)
        return res[old_len:] if old_len > 0 else res

    import multiprocessing as mp
    import uuid

    if temp_dir is None:
        temp_dir = get_app_dir() / "data"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file = temp_dir / f"pi_calc_{uuid.uuid4().hex}.tmp"

    ctx = mp.get_context("spawn")
    result_queue = ctx.Queue()

    p = ctx.Process(
        target=_compute_pi_worker,
        args=(target_len, str(temp_file), result_queue, old_len),
        daemon=True,
    )

    try:
        p.start()
        while p.is_alive():
            if cancel_event is not None and cancel_event.is_set():
                p.terminate()
                p.join(timeout=0.2)
                if temp_file.exists():
                    try:
                        temp_file.unlink()
                    except Exception:
                        pass
                return None
            time.sleep(poll_interval)

        p.join(timeout=1.0)

        # Check if cancellation occurred during join
        if cancel_event is not None and cancel_event.is_set():
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass
            return None

        if temp_file.exists():
            with open(temp_file, "r", encoding="ascii") as f:
                digits = f.read()
            try:
                temp_file.unlink()
            except Exception:
                pass
            return digits

        # Fallback if queue has error or temp file missing
        if not result_queue.empty():
            res = result_queue.get_nowait()
            if not res.get("success", False):
                full = compute_pi_digits(target_len)
                return full[old_len:] if old_len > 0 else full

        full = compute_pi_digits(target_len)
        return full[old_len:] if old_len > 0 else full

    except Exception:
        if p.is_alive():
            try:
                p.terminate()
            except Exception:
                pass
        if cancel_event is not None and cancel_event.is_set():
            return None
        full = compute_pi_digits(target_len)
        return full[old_len:] if old_len > 0 else full


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


class SearchEngineMode(str, Enum):
    STANDARD_ENGINE = "standard"
    BINARY_INDEX_ENGINE = "binary_index"


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
    engine_mode: str = "Standard Engine"


class PiEngine:
    """
    Persistent calculation and search engine for Pi digits.
    """

    def __init__(
        self,
        data_dir: str | Path | None = None,
        default_seed_digits: int = 100_000_000,
        load_seed_pack: Optional[bool] = None,
        seed_dir: str | Path | None = None,
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

        self.custom_seed_dir = Path(seed_dir).resolve() if seed_dir is not None else None

        if load_seed_pack is None:
            # Load seed pack by default in production or whenever data_dir is standard data/ or custom seed_dir provided
            if self.custom_seed_dir is not None or data_dir is None or str(data_dir).rstrip("/\\") in ("data", str(get_app_dir() / "data")):
                self._should_load_seed = True
            else:
                self._should_load_seed = (default_seed_digits >= 100_000_000)
        else:
            self._should_load_seed = load_seed_pack

        self.digits_file = self.data_dir / "pi_digits.txt"
        self.index_file = self.data_dir / "pi_index.json"
        self.history_file = self.data_dir / "search_history.json"

        self._lock = threading.Lock()
        self._seed_lock = threading.Lock()
        self._sync_seed_thread: Optional[threading.Thread] = None
        self.digits: str = ""
        # Sorted list of (query, position_1based) for fast binary search
        self.query_cache: list[tuple[str, int]] = []
        # Sorted list of (prefix_4digit, first_occurrence_1based)
        self.prefix_index: list[tuple[str, int]] = []

        # Empirical calculation benchmark tracking
        if BACKEND_NAME == "gmpy":
            self.last_calc_digits: int = 100_000_000
            self.last_calc_time: float = 92.0
            self.has_calibrated_benchmark: bool = True
        else:
            self.last_calc_digits = 100_000
            self.last_calc_time = 0.35
            self.has_calibrated_benchmark = False

        # Binary Index (.piidx) engine state
        self.binary_index: Optional[PiBinaryIndex] = None
        self.binary_index_path: Optional[Path] = None

        self._load_state(default_seed_digits)

    def calibrate_benchmark(self, force: bool = False) -> None:
        """
        Runs a quick micro-benchmark (calculating 500,000 digits in memory)
        to calibrate N_last and T_last when starting with small or uncalibrated caches.
        Does not overwrite deep calibrations (>= 10,000,000 digits) unless forced.
        """
        if (self.has_calibrated_benchmark or self.last_calc_digits >= 10_000_000) and not force:
            return

        bench_digits = 500_000
        t_start = time.perf_counter()
        _ = compute_pi_digits(bench_digits)
        elapsed = time.perf_counter() - t_start

        self.last_calc_digits = bench_digits
        self.last_calc_time = max(0.001, elapsed)
        self.has_calibrated_benchmark = True

    def sync_seed_file(self) -> None:
        """
        Updates the local external seed archive with the current self.digits stream.
        GitHub Safety Rule:
        - When frozen (.exe), updates dist/seed/pi_100m.zst (or external seed path).
        - If self.custom_seed_dir is set (e.g. in test suites), updates that seed file.
        - In standard dev mode, does NOT overwrite project root seed/pi_100m.zst to prevent >50MB git commits.
        """
        # If a background sync thread is already running from another thread, wait for it
        if (
            self._sync_seed_thread is not None
            and self._sync_seed_thread.is_alive()
            and threading.current_thread() != self._sync_seed_thread
        ):
            self._sync_seed_thread.join()

        with self._seed_lock:
            target_seed_path: Optional[Path] = None

            if self.custom_seed_dir is not None:
                target_seed_path = self.custom_seed_dir / "pi_100m.zst"
            elif getattr(sys, "frozen", False):
                exe_dir = Path(sys.executable).resolve().parent
                target_seed_path = exe_dir / "seed" / "pi_100m.zst"

            if target_seed_path is not None and len(self.digits) > 0:
                try:
                    compress_seed_file(self.digits, target_seed_path)
                except Exception:
                    pass

    def _sync_seed_in_background(self) -> None:
        """Spawns a non-blocking thread to synchronize the external seed file."""
        t = threading.Thread(target=self.sync_seed_file, daemon=True)
        self._sync_seed_thread = t
        t.start()

    def export_seed(
        self,
        dest_path: str | Path,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """
        Exports the currently cached Pi digits to dest_path.
        Supports:
        - .zst (Zstandard compression)
        - .gz / .txt.gz (Gzip compression)
        - .xz (LZMA compression)
        - .txt (raw ASCII digits)
        Returns metadata dict: {'digits_count': int, 'path': str, 'bytes': int}.
        """
        dest = Path(dest_path).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            digits_to_export = self.digits

        if not digits_to_export:
            raise ValueError("Cannot export empty Pi digits cache.")

        if progress_callback:
            progress_callback("Compressing seed file...")

        name_lower = dest.name.lower()
        if name_lower.endswith((".zst", ".xz", ".gz")):
            compress_seed_file(digits_to_export, dest)
        else:
            import uuid
            temp_dest = dest.parent / f"{dest.name}.{uuid.uuid4().hex}.tmp"
            with open(temp_dest, "w", encoding="ascii") as f:
                f.write(digits_to_export)
            temp_dest.replace(dest)

        file_size = dest.stat().st_size
        gc.collect()
        return {
            "digits_count": len(digits_to_export),
            "path": str(dest),
            "bytes": file_size,
        }

    def import_seed(
        self,
        src_path: str | Path,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> int:
        """
        Imports and validates a seed file (.zst, .xz, .gz, or .txt).
        Validates that:
        1. File exists.
        2. Stream starts with standard Pi sequence '1415926535' (first 10 decimal digits).
        3. Stream contains strict ASCII decimal digits only ('0' through '9').
        Stages verified stream by writing to a temporary .tmp file before atomically swapping
        into self.digits_file via os.replace(), ensuring active cache is never corrupted in-place.
        Updates self.digits, resets query cache, persists state, and syncs external seed archive if running frozen.
        Returns the total digit count imported.
        """
        src = Path(src_path).resolve()
        if not src.exists():
            raise FileNotFoundError(f"Seed file not found: {src}")

        if progress_callback:
            progress_callback("Decompressing seed file...")

        name_lower = src.name.lower()
        if name_lower.endswith((".zst", ".xz", ".gz")):
            try:
                imported_digits = decompress_seed_file(src)
            except Exception as e:
                raise ValueError(f"Malformed or corrupted compressed seed: {e}") from e
        else:
            try:
                with open(src, "r", encoding="utf-8") as f:
                    imported_digits = f.read().strip()
            except UnicodeDecodeError as e:
                raise ValueError(f"Invalid seed file: contains non-ASCII characters ({e})") from e

        if progress_callback:
            progress_callback("Validating Pi sequence...")

        if not imported_digits.startswith("1415926535"):
            raise ValueError("Invalid seed file: does not start with standard Pi digits ('1415926535').")

        if not (imported_digits.isascii() and imported_digits.isdigit()):
            raise ValueError("Invalid seed file: contains non-numeric or non-ASCII characters.")

        digit_count = len(imported_digits)

        if progress_callback:
            progress_callback("Saving imported digits...")

        import uuid
        temp_file = self.digits_file.parent / f"{self.digits_file.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temp_file, "w", encoding="ascii") as f:
                f.write(imported_digits)

            with self._lock:
                os.replace(temp_file, self.digits_file)
                self.digits = imported_digits
                self.query_cache.clear()
                self.prefix_index.clear()
                self._sort_indices()
                self._save_index()
                gc.collect()
        finally:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass

        self._sync_seed_in_background()
        return digit_count

    def _load_state(self, default_seed_digits: int) -> None:
        """Loads cached digits and sorted indices from disk, extracts bundled seed, or seeds initial digits."""
        # Check if digits_file exists and has full seed digits
        file_needs_seed = True
        min_expected = 100_000_000 if self.custom_seed_dir is None else 1
        if self.digits_file.exists():
            try:
                file_size = self.digits_file.stat().st_size
                if file_size >= min_expected:
                    file_needs_seed = False
                    with open(self.digits_file, "r", encoding="ascii") as f:
                        self.digits = f.read().strip()
                else:
                    # File exists but is smaller than expected (e.g. outdated/partial cache)
                    file_needs_seed = True
            except Exception:
                file_needs_seed = True

        # Extract bundled or external seed if file is missing or smaller than expected
        if file_needs_seed and self._should_load_seed:
            seed_path = get_seed_path(self.custom_seed_dir)
            if seed_path is not None:
                try:
                    seed_digits = decompress_seed_file(seed_path)
                    if self.custom_seed_dir is not None or len(seed_digits) >= 100_000_000:
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
                    bench = data.get("benchmark")
                    if isinstance(bench, dict):
                        if "last_calc_digits" in bench and "last_calc_time" in bench:
                            self.last_calc_digits = int(bench["last_calc_digits"])
                            self.last_calc_time = float(bench["last_calc_time"])
                            self.has_calibrated_benchmark = True
            except Exception:
                self.query_cache = []
                self.prefix_index = []

        # Ensure query cache is strictly sorted using Timsort
        self._sort_indices()

        # Auto-discover and load .piidx binary index if present
        candidate_indices = [
            self.data_dir / "pi_10digit.piidx",
            get_app_dir() / "data" / "pi_10digit.piidx",
            get_app_dir() / "seed" / "pi_10digit.piidx",
        ]
        for cand in candidate_indices:
            if cand.exists():
                try:
                    self.load_binary_index(cand)
                    break
                except Exception:
                    pass

    def load_binary_index(self, index_path: str | Path) -> PiBinaryIndex:
        """Loads and memory-maps a .piidx binary index file."""
        with self._lock:
            if self.binary_index is not None:
                self.unload_binary_index()
            idx = PiBinaryIndex(index_path)
            self.binary_index = idx
            self.binary_index_path = idx.path
            gc.collect()
            return idx

    def unload_binary_index(self) -> None:
        """Unloads and unmaps the current binary index."""
        with self._lock:
            if self.binary_index is not None:
                self.binary_index.close()
                self.binary_index = None
                self.binary_index_path = None
                gc.collect()

    def _sort_indices(self) -> None:
        """Sorts query cache using Python's native Timsort."""
        self.query_cache.sort(key=lambda x: x[0])
        self.prefix_index.sort(key=lambda x: x[0])

    def _save_index(self) -> None:
        """Saves sorted query cache and benchmark metrics to disk via atomic swap."""
        data = {
            "query_cache": self.query_cache,
            "prefix_index": self.prefix_index,
            "benchmark": {
                "last_calc_digits": self.last_calc_digits,
                "last_calc_time": self.last_calc_time,
            },
        }
        import uuid
        temp_file = self.index_file.parent / f"{self.index_file.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(temp_file, self.index_file)
        finally:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass

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
            T_est = T_last * ((current_total + chunk_delta) / N_last) ** 1.28

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
        return float(self.last_calc_time * (ratio ** 1.28))

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
            N_target = int(N_last * (target_seconds / T_last) ** (1 / 1.28))
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
        n_target = int(n_last * (ratio ** (1.0 / 1.28)))
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
        # Ensure CPU benchmark is calibrated if dealing with deep caches without baseline
        if current_total >= 10_000_000 and not self.has_calibrated_benchmark and self.last_calc_digits < 10_000_000:
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
        engine_mode: SearchEngineMode | str = SearchEngineMode.STANDARD_ENGINE,
    ) -> Optional[SearchResult]:
        """
        Executes the search strategy:
        - STANDARD_ENGINE: Existing in-memory cached seed + live calculation bursts.
        - BINARY_INDEX_ENGINE: High-speed memory-mapped lookup for 10-digit numbers with automatic fallback rules:
            * 1-9 digits: Falls back to Standard Engine with [Fallback: Standard Engine (1-9 digit query)].
            * 10 digits (Index Hit): Returns exact 1-based position immediately (Hit in X.XX ms) with no context digits.
            * 10 digits (Index Miss / No Index): Falls back to Standard Engine.
        """
        mode_val = str(engine_mode).lower()
        if "binary" in mode_val:
            return self._search_binary_index(
                query=query,
                chunk_size=chunk_size,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
                checkpoint_callback=checkpoint_callback,
            )
        else:
            return self._search_standard(
                query=query,
                chunk_size=chunk_size,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
                checkpoint_callback=checkpoint_callback,
            )

    def _search_binary_index(
        self,
        query: str,
        chunk_size: Optional[int] = None,
        cancel_event: Optional[threading.Event] = None,
        progress_callback: Optional[Callable[[int, float], None]] = None,
        checkpoint_callback: Optional[Callable[..., str | int | None]] = None,
    ) -> Optional[SearchResult]:
        """
        Executes binary index search with automatic fallback rules.
        """
        clean_query = validate_query(query)
        q_len = len(clean_query)
        start_time = time.perf_counter()

        # Case A: 1-9 digits -> Automatic fallback to Standard Engine
        if q_len < 10:
            res = self._search_standard(
                query=clean_query,
                chunk_size=chunk_size,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
                checkpoint_callback=checkpoint_callback,
            )
            if res is not None:
                res.engine_mode = "Binary Index (Fallback: 1-9 Digits)"
                res.source = f"[Fallback: Standard Engine (1-9 digit query)] • {res.source}"
            return res

        # Case C1: No binary index loaded -> Check cached seed, then guard if >= 100M
        if self.binary_index is None:
            local_idx = self.digits.find(clean_query)
            if local_idx != -1:
                global_pos = local_idx + 1
                elapsed = time.perf_counter() - start_time
                self.record_query_match(clean_query, global_pos)
                preceding, target, following = format_context(self.digits, global_pos, len(clean_query))
                return SearchResult(
                    query=clean_query,
                    position=global_pos,
                    preceding=preceding,
                    target=target,
                    following=following,
                    elapsed_seconds=elapsed,
                    source="[No Index Loaded -> Fallback: Standard Engine] • Sorted Index Cache",
                    new_digits_calculated=0,
                    engine_mode="Binary Index (No Index Loaded)",
                )

            cached_digits = len(self.digits)
            if cached_digits >= 100_000_000:
                elapsed = time.perf_counter() - start_time
                cached_m = cached_digits / 1_000_000.0
                coverage = (cached_digits / 10_000_000_000.0) * 100.0
                miss_msg = f"[Index Miss: Not found in current {cached_m:.1f}M cached digits ({coverage:.2f}% coverage)]"
                return SearchResult(
                    query=clean_query,
                    position=0,
                    preceding="",
                    target=clean_query,
                    following="",
                    elapsed_seconds=elapsed,
                    source=miss_msg,
                    new_digits_calculated=0,
                    engine_mode="Binary Index (Miss)",
                )

            res = self._search_standard(
                query=clean_query,
                chunk_size=chunk_size,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
                checkpoint_callback=checkpoint_callback,
            )
            if res is not None:
                res.engine_mode = "Binary Index (No Index Loaded)"
                res.source = f"[No Index Loaded -> Fallback: Standard Engine] • {res.source}"
            return res

        # Binary index is loaded!
        b_idx = self.binary_index
        hit_pos: Optional[int] = None

        # Check cutoff_pos rule for STRAGGLER mode
        if b_idx.mode == BinaryIndexMode.STRAGGLER_SORTED_10B and b_idx.cutoff_pos > 0:
            cutoff = b_idx.cutoff_pos
            scan_limit = min(len(self.digits), cutoff + 9)
            local_idx = self.digits[:scan_limit].find(clean_query)
            if local_idx != -1 and (local_idx + 1) <= cutoff:
                hit_pos = local_idx + 1
            else:
                hit_pos = b_idx.lookup(clean_query)
        else:
            hit_pos = b_idx.lookup(clean_query)

        # Case B: Index Hit!
        if hit_pos is not None:
            elapsed = time.perf_counter() - start_time
            self.record_query_match(clean_query, hit_pos)
            return SearchResult(
                query=clean_query,
                position=hit_pos,
                preceding="",
                target=clean_query,
                following="",
                elapsed_seconds=elapsed,
                source=f"[Engine: 10-Digit Binary Index (Hit in {format_latency(elapsed)})]",
                new_digits_calculated=0,
                engine_mode="Binary Index",
            )

        # Check if present in remaining cached digits
        local_idx = self.digits.find(clean_query)
        if local_idx != -1:
            global_pos = local_idx + 1
            elapsed = time.perf_counter() - start_time
            self.record_query_match(clean_query, global_pos)
            return SearchResult(
                query=clean_query,
                position=global_pos,
                preceding="",
                target=clean_query,
                following="",
                elapsed_seconds=elapsed,
                source=f"[Engine: 10-Digit Binary Index (Hit in cached seed in {format_latency(elapsed)})]",
                new_digits_calculated=0,
                engine_mode="Binary Index",
            )

        # Case C2: Index Miss (number did not appear in scanned range or cached seed)
        cached_digits = max(len(self.digits), b_idx.max_pi_digit_scanned)
        if cached_digits >= 100_000_000:
            elapsed = time.perf_counter() - start_time
            cached_m = cached_digits / 1_000_000.0
            coverage = (cached_digits / 10_000_000_000.0) * 100.0
            miss_msg = f"[Index Miss: Not found in current {cached_m:.1f}M cached digits ({coverage:.2f}% coverage)]"
            return SearchResult(
                query=clean_query,
                position=0,
                preceding="",
                target=clean_query,
                following="",
                elapsed_seconds=elapsed,
                source=miss_msg,
                new_digits_calculated=0,
                engine_mode="Binary Index (Miss)",
            )

        max_scanned = b_idx.max_pi_digit_scanned
        res = self._search_standard(
            query=clean_query,
            chunk_size=chunk_size,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            checkpoint_callback=checkpoint_callback,
        )
        if res is not None:
            res.engine_mode = "Binary Index (Fallback: Miss)"
            res.source = f"[Index Miss (Scanned up to Digit #{max_scanned:,}) -> Fallback: Standard Engine] • {res.source}"
        return res

    def compare_engines(
        self,
        query: str,
        cancel_event: Optional[threading.Event] = None,
        progress_callback: Optional[Callable[..., None]] = None,
        checkpoint_callback: Optional[Callable[..., None]] = None,
    ) -> Tuple[Optional[SearchResult], Optional[SearchResult], Optional[float]]:
        """
        Executes query against both Standard Engine and Binary Index Engine back-to-back.
        Returns (res_standard, res_binary, speedup_factor).
        """
        clean_query = validate_query(query)

        # 1. Standard Engine search
        res_std = self.search(
            clean_query,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            checkpoint_callback=checkpoint_callback,
            engine_mode=SearchEngineMode.STANDARD_ENGINE,
        )
        if cancel_event is not None and cancel_event.is_set():
            return None, None, None

        # 2. Binary Index Engine search
        res_bin = self.search(
            clean_query,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            checkpoint_callback=checkpoint_callback,
            engine_mode=SearchEngineMode.BINARY_INDEX_ENGINE,
        )

        speedup: Optional[float] = None
        if (
            res_std is not None
            and res_bin is not None
            and res_bin.elapsed_seconds > 0
            and res_std.elapsed_seconds > 0
        ):
            speedup = res_std.elapsed_seconds / res_bin.elapsed_seconds

        return res_std, res_bin, speedup

    def _search_standard(
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

        # Immediate Checkpoint Pause on Cache Miss when current_len >= 1,000,000:
        # If sequence was not found in the existing cached stream (e.g. 100M seed),
        # DO NOT automatically begin a massive multi-minute calculation! Prompt checkpoint_callback immediately.
        if current_len >= 1_000_000 and checkpoint_callback is not None:
            tier_cfg = self.get_checkpoint_tier_config(current_len)
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
                action = checkpoint_callback(0, current_len, tier_cfg)
            else:
                action = checkpoint_callback(0, current_len)

            if action == "stop" or action is None:
                return None
            pending_action: Any = action
        elif chunk_size is not None:
            pending_action = chunk_size
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
                    new_slice = compute_pi_digits_cancellable(
                        target_len,
                        old_len=old_total,
                        cancel_event=cancel_event,
                        temp_dir=self.data_dir,
                    )
                    if new_slice is None:
                        return None
                    calc_duration = time.perf_counter() - t_calc_start

                    self.last_calc_digits = target_len
                    self.last_calc_time = max(0.001, calc_duration)
                    new_digits_total += len(new_slice)

                    with self._lock:
                        self.digits += new_slice
                        with open(self.digits_file, "a", encoding="ascii") as f:
                            f.write(new_slice)
                        self._save_index()
                    self._sync_seed_in_background()

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
                new_slice = compute_pi_digits_cancellable(
                    target_len,
                    old_len=old_total,
                    cancel_event=cancel_event,
                    temp_dir=self.data_dir,
                )
                if new_slice is None:
                    return None
                calc_duration = time.perf_counter() - t_calc_start

                self.last_calc_digits = target_len
                self.last_calc_time = max(0.001, calc_duration)
                new_digits_total += len(new_slice)

                with self._lock:
                    self.digits += new_slice
                    with open(self.digits_file, "a", encoding="ascii") as f:
                        f.write(new_slice)
                    self._save_index()
                    del new_slice
                    gc.collect()
                self._sync_seed_in_background()

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

    def _expand_to_length(self, target_len: int, cancel_event: Optional[threading.Event] = None) -> None:
        """Expands self.digits to at least target_len digits and saves to disk."""
        old_total = len(self.digits)
        if old_total < target_len:
            with self._lock:
                t_calc_start = time.perf_counter()
                new_slice = compute_pi_digits_cancellable(
                    target_len,
                    old_len=old_total,
                    cancel_event=cancel_event,
                    temp_dir=self.data_dir,
                )
                if new_slice is not None:
                    self.digits += new_slice
                    calc_duration = time.perf_counter() - t_calc_start
                    self.last_calc_digits = target_len
                    self.last_calc_time = max(0.001, calc_duration)
                    with open(self.digits_file, "a", encoding="ascii") as f:
                        f.write(new_slice)
                    self._save_index()
                    del new_slice
                    gc.collect()
            self._sync_seed_in_background()

    def expand_seed(
        self,
        target_len: int,
        progress_callback: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> int:
        """
        Public method to dynamically expand the persistent seed cache to target_len digits.
        Executes cancellable calculation, appends delta to disk, updates indices, and syncs seed.
        Returns the new total cached digit count.
        """
        old_total = len(self.digits)
        if target_len <= old_total:
            return old_total

        if progress_callback:
            progress_callback(f"Expanding Pi cache from {old_total:,} to {target_len:,} digits...")

        self._expand_to_length(target_len, cancel_event=cancel_event)
        gc.collect()
        return len(self.digits)

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
        
        import uuid
        temp_file = self.history_file.parent / f"{self.history_file.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2)
            os.replace(temp_file, self.history_file)
        finally:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass

