"""
Test suite for PiEngine (PiPhone backend).
Covers TC-01 through TC-07 as specified in TEST_PLAN.md.
"""

import threading
import time
from pathlib import Path

import pytest
from pi_engine import (
    DEFAULT_CHUNK_SIZE,
    MAX_QUERY_LENGTH,
    PiEngine,
    SearchResult,
    compute_pi_digits,
    format_context,
    validate_query,
)


@pytest.fixture
def temp_engine(tmp_path: Path) -> PiEngine:
    """Fixture providing a fresh PiEngine instance backed by a temporary directory."""
    # Seed with 10,000 digits to allow fast local testing without network/heavy math
    return PiEngine(data_dir=tmp_path, default_seed_digits=10_000)


# =====================================================================
# TC-01: Start-of-Pi & "3." Padding (Search "1415" at Position 1)
# =====================================================================
def test_tc01_start_of_pi_3_padding(temp_engine: PiEngine):
    """
    TC-01: Verify that searching '1415' finds position 1,
    with preceding context '3.', target '1415', and following '9265'.
    """
    res = temp_engine.search("1415")
    assert res is not None
    assert res.position == 1
    assert res.target == "1415"
    assert res.preceding == "3."
    assert res.following == "9265"


# =====================================================================
# TC-02: Partial "3." Padding (Search "4159" at Position 2)
# =====================================================================
def test_tc02_partial_3_padding(temp_engine: PiEngine):
    """
    TC-02: Verify that searching '4159' finds position 2,
    with preceding context '3.1', target '4159', and following '2653'.
    """
    res = temp_engine.search("4159")
    assert res is not None
    assert res.position == 2
    assert res.target == "4159"
    assert res.preceding == "3.1"
    assert res.following == "2653"


# =====================================================================
# TC-03: Standard 4-Digit Match & Context (Search "9265" at Position 5)
# =====================================================================
def test_tc03_standard_4_digit_match_and_context(temp_engine: PiEngine):
    """
    TC-03: Verify that searching '9265' finds position 5,
    with preceding context '1415', target '9265', and following '3589'.
    """
    res = temp_engine.search("9265")
    assert res is not None
    assert res.position == 5
    assert res.target == "9265"
    assert res.preceding == "1415"
    assert res.following == "3589"


# =====================================================================
# TC-04: Input Validation & 11th Digit Block
# =====================================================================
def test_tc04_input_validation_and_11th_digit_block():
    """
    TC-04: Verify inputs of length 1 to 10 digits are accepted,
    non-numeric characters are rejected, and any 11th digit is blocked.
    """
    # Valid inputs
    assert validate_query("1") == "1"
    assert validate_query("1415") == "1415"
    assert validate_query("8675309") == "8675309"
    assert validate_query("1234567890") == "1234567890"  # exactly 10 digits

    # Empty and whitespace
    with pytest.raises(ValueError, match="cannot be empty"):
        validate_query("")
    with pytest.raises(ValueError, match="cannot be empty"):
        validate_query("   ")

    # Non-numeric characters
    with pytest.raises(ValueError, match="digits only"):
        validate_query("123a")
    with pytest.raises(ValueError, match="digits only"):
        validate_query("+15550199")
    with pytest.raises(ValueError, match="digits only"):
        validate_query("123-456")
    with pytest.raises(ValueError, match="digits only"):
        validate_query("3.1415")

    # 11th digit block (length > 10)
    with pytest.raises(ValueError, match=f"maximum length of {MAX_QUERY_LENGTH}"):
        validate_query("12345678901")  # 11 digits


# =====================================================================
# TC-05: Timsort & Binary Search Cache Hit
# =====================================================================
def test_tc05_timsort_and_binary_search_cache_hit(temp_engine: PiEngine):
    """
    TC-05: Verify that searching a sequence a second time resolves via
    Binary Search (bisect) against the Timsorted query cache with 0 new
    digits calculated and instantaneous response.
    """
    query = "9265"
    # First search: may resolve from cached digits or newly generated
    res1 = temp_engine.search(query)
    assert res1 is not None

    # Check cache contains the query and is sorted
    assert temp_engine.lookup_cache(query) == res1.position
    # Verify sorting
    cache_keys = [q for q, _ in temp_engine.query_cache]
    assert cache_keys == sorted(cache_keys)

    # Second search: MUST be a cache hit
    res2 = temp_engine.search(query)
    assert res2 is not None
    assert res2.source == "Sorted Index Cache"
    assert res2.new_digits_calculated == 0
    assert res2.position == res1.position
    assert res2.elapsed_seconds < 0.05  # sub-millisecond lookup


# =====================================================================
# TC-06: Chunk Boundary Overlap Detection
# =====================================================================
def test_tc06_chunk_boundary_overlap(tmp_path: Path):
    """
    TC-06: Verify that a sequence straddling the boundary between two
    calculation chunks is accurately detected without missing digits.
    """
    # Create engine with small initial seed (e.g., 500 digits)
    engine = PiEngine(data_dir=tmp_path, default_seed_digits=500)
    initial_digits = engine.digits

    # Take a 6-digit sequence that straddles index 500:
    # 3 digits from the end of chunk 1, 3 digits from the start of chunk 2.
    next_digits = compute_pi_digits(1000)
    # Target spans digits [497:503] (0-based) -> 1-based pos 498
    target_sequence = next_digits[497:503]

    # Reset engine digits to exactly 500 to simulate boundary
    engine.digits = initial_digits[:500]
    with open(engine.digits_file, "w", encoding="ascii") as f:
        f.write(engine.digits)

    # Search for straddling target sequence with chunk_size=500
    res = engine.search(target_sequence, chunk_size=500)
    assert res is not None
    assert res.position == 498
    assert res.target == target_sequence


# =====================================================================
# TC-07: Background Calculation Cancellation
# =====================================================================
def test_tc07_background_calculation_cancellation(tmp_path: Path):
    """
    TC-07: Verify that triggering a stop event cleanly halts the
    calculation thread, preserves digits calculated up to that point,
    and returns None cleanly.
    """
    engine = PiEngine(data_dir=tmp_path, default_seed_digits=100)
    cancel_event = threading.Event()

    result_container: list[SearchResult | None] = []

    def worker():
        # Search for a 10-digit number unlikely to exist in first few hundred digits,
        # with chunk_size 10,000 to induce multi-chunk expansion
        res = engine.search("9999999999", chunk_size=10_000, cancel_event=cancel_event)
        result_container.append(res)

    worker_thread = threading.Thread(target=worker)
    worker_thread.start()

    # Cancel immediately
    time.sleep(0.02)
    cancel_event.set()
    worker_thread.join(timeout=2.0)

    assert not worker_thread.is_alive(), "Worker thread should terminate cleanly upon cancel."
    assert len(result_container) == 1
    assert result_container[0] is None, "Result should be None when search is cancelled."
    # Cached digits file should exist and not be empty or corrupted
    assert len(engine.digits) >= 100


# =====================================================================
# TC-08: Chudnovsky Engine Accuracy Verification
# =====================================================================
def test_tc08_chudnovsky_precision():
    """
    TC-08: Verify first 50 digits of computed Pi match the standard
    mathematical reference value.
    """
    reference_50 = "14159265358979323846264338327950288419716939937510"
    computed = compute_pi_digits(50)
    assert computed == reference_50


# =====================================================================
# TC-09: Persistence and Cold Start Reload
# =====================================================================
def test_tc09_persistence_and_cold_start(tmp_path: Path):
    """
    TC-09: Verify that computed digits and sorted query indices persist to
    disk and are seamlessly reloaded on cold start without recalculation.
    """
    engine1 = PiEngine(data_dir=tmp_path, default_seed_digits=2000)
    res = engine1.search("1415")
    assert res is not None

    # Instantiate fresh engine instance pointing to the same data directory
    engine2 = PiEngine(data_dir=tmp_path, default_seed_digits=0)
    assert len(engine2.digits) == len(engine1.digits)
    assert engine2.lookup_cache("1415") == 1

