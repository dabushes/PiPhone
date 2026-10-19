"""
Test suite for PiEngine (PiPhone backend).
Covers TC-01 through TC-11 (plus Chudnovsky precision and cold start persistence).
"""

import threading
import time
from pathlib import Path

import pytest
from pi_engine import (
    DEFAULT_CHUNK_SIZE,
    MAX_QUERY_LENGTH,
    CheckpointOption,
    CheckpointTierConfig,
    PiEngine,
    SearchResult,
    compute_pi_digits,
    format_context,
    format_delta_digits,
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
    with preceding context '3.', target '1415', and following '9265...'.
    """
    res = temp_engine.search("1415")
    assert res is not None
    assert res.position == 1
    assert res.target == "1415"
    assert res.preceding == "3."
    assert res.following == "9265..."


# =====================================================================
# TC-02: Partial "3." Padding (Search "4159" at Position 2)
# =====================================================================
def test_tc02_partial_3_padding(temp_engine: PiEngine):
    """
    TC-02: Verify that searching '4159' finds position 2,
    with preceding context '3.1', target '4159', and following '2653...'.
    """
    res = temp_engine.search("4159")
    assert res is not None
    assert res.position == 2
    assert res.target == "4159"
    assert res.preceding == "3.1"
    assert res.following == "2653..."


# =====================================================================
# TC-03: Standard 4-Digit Match & Context (Search "9265" at Position 5)
# =====================================================================
def test_tc03_standard_4_digit_match_and_context(temp_engine: PiEngine):
    """
    TC-03: Verify that searching '9265' finds position 5,
    with preceding context '...1415', target '9265', and following '3589...'.
    """
    res = temp_engine.search("9265")
    assert res is not None
    assert res.position == 5
    assert res.target == "9265"
    assert res.preceding == "...1415"
    assert res.following == "3589..."


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
    # First search: resolves from cached digits or generates
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
# TC-06: Chunk Boundary Overlap Detection & Safety Lookahead
# =====================================================================
def test_tc06_chunk_boundary_overlap(tmp_path: Path):
    """
    TC-06: Verify that:
    1. A 7-digit sequence straddling the boundary between two calculation
       chunks (3 digits at end of chunk 1, 4 digits at start of chunk 2)
       is accurately detected with exact 1-based position.
    2. A sequence matching on the terminal digit of a chunk automatically
       triggers lookahead extension so all 4 following digits are available.
    3. 4-digit prefixes straddling chunk boundaries are incrementally recorded
       in the prefix index.
    """
    # Create engine with initial seed of 500 digits
    engine = PiEngine(data_dir=tmp_path, default_seed_digits=500)
    initial_digits = engine.digits
    assert len(initial_digits) == 500

    next_digits = compute_pi_digits(1000)

    # Part 1: 7-digit sequence straddling boundary index 500 (digits [497:504])
    # 3 digits in chunk 1 (indices 497, 498, 499) + 4 digits in chunk 2 (indices 500, 501, 502, 503)
    target_7 = next_digits[497:504]
    assert len(target_7) == 7

    res = engine.search(target_7, chunk_size=500)
    assert res is not None
    assert res.position == 498
    assert res.target == target_7
    assert len(res.preceding) > 0
    assert len(res.following) == 7  # 4 digits + '...'
    assert not res.following.startswith(".")

    # Part 2: Verify prefix index contains boundary-straddling prefixes
    prefix_straddle = next_digits[497:501]
    assert engine.lookup_prefix(prefix_straddle) == 498

    # Part 3: Match on terminal boundary of chunk (lookahead safety check)
    # '4201989' occurs at index 993..999, landing on the terminal digit (1000) of a 1000-digit chunk.
    engine_terminal = PiEngine(data_dir=tmp_path / "term", default_seed_digits=500)
    term_seq = next_digits[993:1000]  # '4201989'
    res_term = engine_terminal.search(term_seq, chunk_size=500)
    assert res_term is not None
    assert res_term.position == 994
    assert res_term.target == term_seq
    # Verify that lookahead automatically expanded digits so all 4 following digits exist
    assert len(res_term.following) == 7  # 4 trailing digits + '...'
    assert res_term.following == "3809..."
    assert len(engine_terminal.digits) >= 1004


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
        res = engine.search("9999999999", chunk_size=10_000, cancel_event=cancel_event)
        result_container.append(res)

    worker_thread = threading.Thread(target=worker)
    worker_thread.start()

    time.sleep(0.02)
    cancel_event.set()
    worker_thread.join(timeout=2.0)

    assert not worker_thread.is_alive(), "Worker thread should terminate cleanly upon cancel."
    assert len(result_container) == 1
    assert result_container[0] is None, "Result should be None when search is cancelled."
    assert len(engine.digits) >= 100


# =====================================================================
# TC-08: Ellipsis Formatting - Standard (Position >= 5)
# =====================================================================
def test_tc08_ellipsis_formatting_standard(temp_engine: PiEngine):
    """
    TC-08: Verify that searching a sequence at position >= 5 (e.g. '9265' at pos 5)
    formats preceding context with leading '...' ('...1415') and following context
    with trailing '...' ('3589...').
    """
    res = temp_engine.search("9265")
    assert res is not None
    assert res.position == 5
    assert res.preceding == "...1415"
    assert res.target == "9265"
    assert res.following == "3589..."

    # Direct helper test with format_context
    pre, tgt, flw = format_context(temp_engine.digits, pos_1based=5, query_len=4, include_ellipses=True)
    assert pre == "...1415"
    assert tgt == "9265"
    assert flw == "3589..."


# =====================================================================
# TC-09: Ellipsis Formatting - Start of Pi "3." Edge Case (Positions 1-4)
# =====================================================================
def test_tc09_ellipsis_formatting_start_of_pi_edge_case(temp_engine: PiEngine):
    """
    TC-09: Verify that searching sequences starting at decimal positions 1 through 4
    omits the leading '...' before '3.' (since nothing precedes '3.'), while retaining
    the trailing '...' after the following 4 digits.
    """
    # Pos 1: '1415' -> preceding '3.', following '9265...'
    res1 = temp_engine.search("1415")
    assert res1 is not None
    assert res1.position == 1
    assert res1.preceding == "3."
    assert not res1.preceding.startswith("...")
    assert res1.following == "9265..."

    # Pos 2: '4159' -> preceding '3.1', following '2653...'
    res2 = temp_engine.search("4159")
    assert res2 is not None
    assert res2.position == 2
    assert res2.preceding == "3.1"
    assert not res2.preceding.startswith("...")
    assert res2.following == "2653..."

    # Pos 3: '1592' -> preceding '3.14', following '6535...'
    res3 = temp_engine.search("1592")
    assert res3 is not None
    assert res3.position == 3
    assert res3.preceding == "3.14"
    assert not res3.preceding.startswith("...")
    assert res3.following == "6535..."

    # Pos 4: '5926' -> preceding '3.141', following '5358...'
    res4 = temp_engine.search("5926")
    assert res4 is not None
    assert res4.position == 4
    assert res4.preceding == "3.141"
    assert not res4.preceding.startswith("...")
    assert res4.following == "5358..."


# =====================================================================
# TC-10: Checkpoint Pause & Resume Callback (100k / 1M Stepping)
# =====================================================================
def test_tc10_checkpoint_pause_and_resume_callback(tmp_path: Path):
    """
    TC-10: Verify that when a search misses existing cache, the engine triggers
    the checkpoint callback after 100,000 newly calculated digits, can dynamically
    switch step size to 1,000,000, and cleanly stops when given 'stop' signal.
    """
    engine = PiEngine(data_dir=tmp_path, default_seed_digits=500)
    initial_cached = engine.get_total_cached_digits()
    assert initial_cached == 500

    callback_records: list[tuple[int, int]] = []

    def checkpoint_handler(new_digits_total: int, total_cached: int):
        callback_records.append((new_digits_total, total_cached))
        if len(callback_records) == 1:
            # First pause: step size was 100,000 -> switch to 1,000,000
            return 1_000_000
        else:
            # Second pause: halt search
            return "stop"

    # Search for an impossible 10-digit number in first 1.2M digits
    res = engine.search(
        "9999999999",
        chunk_size=100_000,
        checkpoint_callback=checkpoint_handler,
    )

    # Search was stopped by callback
    assert res is None
    assert len(callback_records) == 2

    # First checkpoint: calculated 100,000 new digits -> total 100,500
    assert callback_records[0] == (100_000, 100_500)

    # Second checkpoint: calculated 100,000 + 1,000,000 = 1,100,000 -> total 1,100,500
    assert callback_records[1] == (1_100_000, 1_100_500)

    # Engine persisted all digits to disk up to checkpoint
    assert engine.get_total_cached_digits() == 1_100_500


# =====================================================================
# TC-11: Total Cached Digits Reporting
# =====================================================================
def test_tc11_total_cached_digits_reporting(tmp_path: Path):
    """
    TC-11: Verify engine accurately reports total count of cached decimal digits
    before and after chunk expansion, and reloads it accurately on startup.
    """
    engine = PiEngine(data_dir=tmp_path, default_seed_digits=2000)
    assert engine.get_total_cached_digits() == 2000

    # Expand digits
    engine._expand_to_length(15_000)
    assert engine.get_total_cached_digits() == 15_000

    # Cold restart
    engine_reload = PiEngine(data_dir=tmp_path, default_seed_digits=0)
    assert engine_reload.get_total_cached_digits() == 15_000


# =====================================================================
# TC-12: Empirical Time Estimation Scaling
# =====================================================================
def test_tc12_empirical_time_estimation_scaling(tmp_path: Path):
    """
    TC-12: Verify estimate_chunk_time() returns monotonically increasing estimates
    as current_total and chunk_delta grow, properly calibrates from recorded metrics,
    and format_estimated_time() formats human-readable labels correctly.
    """
    engine = PiEngine(data_dir=tmp_path, default_seed_digits=1000)

    # Test baseline calibration
    engine.last_calc_digits = 100_000
    engine.last_calc_time = 0.10

    # Monotonicity with respect to current_total
    est_100k_total = engine.estimate_chunk_time(100_000, 100_000)
    est_500k_total = engine.estimate_chunk_time(500_000, 100_000)
    est_1m_total = engine.estimate_chunk_time(1_000_000, 100_000)
    assert est_100k_total < est_500k_total < est_1m_total

    # Monotonicity with respect to chunk_delta
    est_delta_50k = engine.estimate_chunk_time(100_000, 50_000)
    est_delta_100k = engine.estimate_chunk_time(100_000, 100_000)
    est_delta_1m = engine.estimate_chunk_time(100_000, 1_000_000)
    assert est_delta_50k < est_delta_100k < est_delta_1m

    # Mathematical precision of scaling law: T_est = 0.10 * (200k / 100k)^1.35
    expected_2x = 0.10 * (2.0 ** 1.35)
    assert abs(est_100k_total - expected_2x) < 0.001

    # Format helpers
    assert PiEngine.format_estimated_time(0.04) == "[< 0.1s]"
    assert PiEngine.format_estimated_time(1.42) == "[~1.4s]"
    assert PiEngine.format_estimated_time(42.1) == "[~42s]"
    assert PiEngine.format_estimated_time(90.0) == "[~1.5m]"


# =====================================================================
# TC-13: 60-Second Time-Ceiling Rule
# =====================================================================
def test_tc13_60_second_threshold_rule(tmp_path: Path):
    """
    TC-13: Verify that should_show_1m_step() returns True when the estimated
    time for +1,000,000 digits is <= 60.0s, and False when > 60.0s.
    """
    engine = PiEngine(data_dir=tmp_path, default_seed_digits=1000)

    # Fast / baseline state: 1M digits estimated at ~1-2 seconds
    engine.last_calc_digits = 100_000
    engine.last_calc_time = 0.05
    assert engine.estimate_chunk_time(100_000, 1_000_000) <= 60.0
    assert engine.should_show_1m_step(100_000) is True

    # Deep calculation state where 1M would exceed 60.0s
    engine.last_calc_digits = 100_000
    engine.last_calc_time = 5.0  # Slow machine / huge base
    # Target = 20,000,000 + 1,000,000 = 21,000,000 digits -> ratio = 210 -> 210^1.35 * 5.0 >> 60s
    est_huge = engine.estimate_chunk_time(20_000_000, 1_000_000)
    assert est_huge > 60.0
    assert engine.should_show_1m_step(20_000_000) is False


# =====================================================================
# TC-14: Inverse Time-Budget Estimator (estimate_digits_for_time)
# =====================================================================
def test_tc14_inverse_time_budget_estimator(tmp_path: Path):
    """
    TC-14: Verify that estimate_digits_for_time():
    1. Accurately inverts estimate_chunk_time() within rounding tolerance.
    2. Rounds result to the nearest 100,000 digits.
    3. Strictly enforces a floor of at least 100,000 digits.
    """
    engine = PiEngine(data_dir=tmp_path, default_seed_digits=1000)
    engine.last_calc_digits = 100_000
    engine.last_calc_time = 0.10

    current_total = 100_000
    desired_delta = 500_000
    t_chunk = engine.estimate_chunk_time(current_total, desired_delta)

    # Invert back to digits
    computed_delta = engine.estimate_digits_for_time(current_total, t_chunk)
    assert computed_delta == desired_delta

    # Verify minimum floor of 100,000 digits even for near-zero time budget
    assert engine.estimate_digits_for_time(current_total, 0.0001) == 100_000
    assert engine.estimate_digits_for_time(current_total, -10.0) == 100_000

    # Verify rounding to nearest 100,000
    delta_arb = engine.estimate_digits_for_time(current_total, 0.5)
    assert delta_arb % 100_000 == 0


# =====================================================================
# TC-15: 4-Tier Checkpoint Schedule (get_checkpoint_tier_config)
# =====================================================================
def test_tc15_4_tier_checkpoint_schedule(tmp_path: Path):
    """
    TC-15: Verify get_checkpoint_tier_config() across all 4 speed tiers:
    - Tier 1: T_1M < 15s -> Continue (+1,000,000), Run ~30s Burst
    - Tier 2: 15s <= T_1M < 30s -> Continue (+1,000,000), Run ~60s Burst
    - Tier 3: 30s <= T_1M <= 60s -> Run ~1 Min Burst, Run ~2 Min Burst (+1M retired)
    - Tier 4: T_1M > 60s -> Run ~2 Min Burst, Run ~5 Min Burst
    """
    engine = PiEngine(data_dir=tmp_path, default_seed_digits=1000)

    # --- Tier 1 (T_1M < 15s) ---
    engine.last_calc_digits = 1_000_000
    engine.last_calc_time = 2.5
    # current_total = 1_000_000, +1M -> target 2M -> ratio 2 -> 2.5 * 2^1.35 ~ 6.37s (< 15s)
    cfg1 = engine.get_checkpoint_tier_config(1_000_000)
    assert cfg1.tier == 1
    assert "Continue (+1,000,000)" in cfg1.option1.label
    assert cfg1.option1.delta_digits == 1_000_000
    assert "Run ~30s Burst" in cfg1.option2.label
    assert cfg1.option2.target_seconds == 30.0

    # --- Tier 2 (15s <= T_1M < 30s) ---
    engine.last_calc_digits = 1_000_000
    engine.last_calc_time = 8.0
    # current_total = 1_000_000, +1M -> target 2M -> ratio 2 -> 8.0 * 2^1.35 ~ 20.4s
    cfg2 = engine.get_checkpoint_tier_config(1_000_000)
    assert cfg2.tier == 2
    assert "Continue (+1,000,000)" in cfg2.option1.label
    assert cfg2.option1.delta_digits == 1_000_000
    assert "Run ~60s Burst" in cfg2.option2.label
    assert cfg2.option2.target_seconds == 60.0

    # --- Tier 3 (30s <= T_1M <= 60s) ---
    engine.last_calc_digits = 1_000_000
    engine.last_calc_time = 18.0
    # current_total = 1_000_000, +1M -> target 2M -> ratio 2 -> 18.0 * 2^1.35 ~ 45.9s
    cfg3 = engine.get_checkpoint_tier_config(1_000_000)
    assert cfg3.tier == 3
    # +1,000,000 button retired
    assert "Continue (+1,000,000)" not in cfg3.option1.label
    assert "Run ~1 Min Burst" in cfg3.option1.label
    assert cfg3.option1.target_seconds == 60.0
    assert "Run ~2 Min Burst" in cfg3.option2.label
    assert cfg3.option2.target_seconds == 120.0

    # --- Tier 4 (T_1M > 60s) ---
    engine.last_calc_digits = 1_000_000
    engine.last_calc_time = 35.0
    # current_total = 1_000_000, +1M -> target 2M -> ratio 2 -> 35.0 * 2^1.35 ~ 89.2s (> 60s)
    cfg4 = engine.get_checkpoint_tier_config(1_000_000)
    assert cfg4.tier == 4
    assert "Run ~2 Min Burst" in cfg4.option1.label
    assert cfg4.option1.target_seconds == 120.0
    assert "Run ~5 Min Burst" in cfg4.option2.label
    assert cfg4.option2.target_seconds == 300.0


# =====================================================================
# TC-16: Chudnovsky Engine Accuracy Verification
# =====================================================================
def test_tc16_chudnovsky_precision():
    """
    TC-16: Verify first 50 digits of computed Pi match the standard
    mathematical reference value.
    """
    reference_50 = "14159265358979323846264338327950288419716939937510"
    computed = compute_pi_digits(50)
    assert computed == reference_50


# =====================================================================
# TC-17: Persistence and Cold Start Reload
# =====================================================================
def test_tc17_persistence_and_cold_start(tmp_path: Path):
    """
    TC-17: Verify that computed digits and sorted query indices persist to
    disk and are seamlessly reloaded on cold start without recalculation.
    """
    engine1 = PiEngine(data_dir=tmp_path, default_seed_digits=2000)
    res = engine1.search("1415")
    assert res is not None

    engine2 = PiEngine(data_dir=tmp_path, default_seed_digits=0)
    assert engine2.get_total_cached_digits() == engine1.get_total_cached_digits()
    assert engine2.lookup_cache("1415") == 1


# =====================================================================
# TC-18: Initial 1,000,000-Digit Burst on Cache Miss
# =====================================================================
def test_tc18_initial_burst_on_cache_miss(tmp_path: Path):
    """
    TC-18: Verify that when search() is called with chunk_size=None (default)
    on a cache miss with fewer than 1,000,000 digits cached, the initial step
    calculates exactly up to 1,000,000 digits before the first checkpoint.
    """
    engine = PiEngine(data_dir=tmp_path, default_seed_digits=100_000)
    assert engine.get_total_cached_digits() == 100_000

    checkpoints = []

    def mock_cb(new_digits: int, total_cached: int, tier_cfg: CheckpointTierConfig):
        checkpoints.append((new_digits, total_cached, tier_cfg.tier))
        return "stop"

    res = engine.search("999999999", chunk_size=None, checkpoint_callback=mock_cb)
    assert res is None
    assert len(checkpoints) == 1
    new_d, total_c, tier = checkpoints[0]
    assert new_d == 900_000
    assert total_c == 1_000_000
    assert tier == 1
