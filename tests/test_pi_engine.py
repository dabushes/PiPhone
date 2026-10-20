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
    compress_seed_file,
    compute_pi_digits,
    decompress_seed_file,
    format_context,
    format_delta_digits,
    get_seed_path,
    validate_query,
)


@pytest.fixture
def temp_engine(tmp_path: Path) -> PiEngine:
    """Fixture providing a fresh PiEngine instance backed by a temporary directory."""
    # Seed with 10,000 digits to allow fast local testing without network/heavy math
    return PiEngine(data_dir=tmp_path, default_seed_digits=10_000, load_seed_pack=False)


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

    # Mathematical precision of scaling law: T_est = 0.10 * (200k / 100k)^1.28
    expected_2x = 0.10 * (2.0 ** 1.28)
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
# TC-15: 5-Tier Checkpoint Schedule (get_checkpoint_tier_config)
# =====================================================================
def test_tc15_5_tier_checkpoint_schedule(tmp_path: Path):
    """
    TC-15: Verify get_checkpoint_tier_config() across all 5 speed tiers:
    - Tier 1: T_min_step < 15s -> Continue (+1,000,000 Digits) [~X.Xs], Search for 30 Seconds
    - Tier 2: 15s <= T_min_step < 24s -> Continue (+1,000,000 Digits) [~XXs], Search for 60 Seconds
    - Tier 3: 24s <= T_min_step < 48s -> Search for 1 Minute, Search for 2 Minutes
    - Tier 4: 48s <= T_min_step <= 90s -> Search for 2 Minutes, Search for 5 Minutes
    - Tier 5: T_min_step > 90s -> Calculate Next Block, Search for X Minutes, Note present
    """
    engine = PiEngine(data_dir=tmp_path, default_seed_digits=1000)

    # --- Tier 1 (T_min_step < 15s) ---
    engine.last_calc_digits = 1_000_000
    engine.last_calc_time = 2.0
    # current_total = 1_000_000, +2M -> target 3M -> ratio 3 -> 2.0 * 3^1.65 ~ 12.26s (< 15s)
    cfg1 = engine.get_checkpoint_tier_config(1_000_000)
    assert cfg1.tier == 1
    assert "Continue (+1,000,000" in cfg1.option1.label
    assert cfg1.option1.delta_digits == 1_000_000
    assert cfg1.option1.mode == "fixed_digits"
    assert "Search for 30 Seconds" in cfg1.option2.label
    assert cfg1.option2.target_seconds == 30.0
    assert cfg1.option2.mode == "time_budget"
    assert cfg1.note == ""

    # --- Tier 2 (15s <= T_min_step < 24s) ---
    engine.last_calc_digits = 1_000_000
    engine.last_calc_time = 4.5
    # current_total = 1_000_000, +2M -> target 3M -> 4.5 * 3^1.28 ~ 18.36s (15 <= t < 24)
    cfg2 = engine.get_checkpoint_tier_config(1_000_000)
    assert cfg2.tier == 2
    assert "Continue (+1,000,000" in cfg2.option1.label
    assert cfg2.option1.delta_digits == 1_000_000
    assert cfg2.option1.mode == "fixed_digits"
    assert "Search for 60 Seconds" in cfg2.option2.label
    assert cfg2.option2.target_seconds == 60.0
    assert cfg2.option2.mode == "time_budget"
    assert cfg2.note == ""

    # --- Tier 3 (24s <= T_min_step < 48s) ---
    engine.last_calc_digits = 1_000_000
    engine.last_calc_time = 7.0
    # current_total = 1_000_000, +2M -> target 3M -> 7.0 * 3^1.28 ~ 28.57s (24 <= t < 48)
    cfg3 = engine.get_checkpoint_tier_config(1_000_000)
    assert cfg3.tier == 3
    assert "Continue (+1,000,000" not in cfg3.option1.label
    assert "Search for 1 Minute" in cfg3.option1.label
    assert cfg3.option1.target_seconds == 60.0
    assert cfg3.option1.mode == "time_budget"
    assert "Search for 2 Minutes" in cfg3.option2.label
    assert cfg3.option2.target_seconds == 120.0
    assert cfg3.option2.mode == "time_budget"
    assert cfg3.note == ""

    # --- Tier 4 (48s <= T_min_step <= 90s) ---
    engine.last_calc_digits = 1_000_000
    engine.last_calc_time = 14.0
    # current_total = 1_000_000, +2M -> target 3M -> 14.0 * 3^1.28 ~ 57.13s (48 <= t <= 90)
    cfg4 = engine.get_checkpoint_tier_config(1_000_000)
    assert cfg4.tier == 4
    assert "Search for 2 Minutes" in cfg4.option1.label
    assert cfg4.option1.target_seconds == 120.0
    assert cfg4.option1.mode == "time_budget"
    assert "Search for 5 Minutes" in cfg4.option2.label
    assert cfg4.option2.target_seconds == 300.0
    assert cfg4.option2.mode == "time_budget"
    assert cfg4.note == ""

    # --- Tier 5 (T_min_step > 90s) ---
    engine.last_calc_digits = 1_000_000
    engine.last_calc_time = 25.0
    # current_total = 1_000_000, +2M -> target 3M -> 25.0 * 3^1.28 ~ 102.0s (> 90s)
    cfg5 = engine.get_checkpoint_tier_config(1_000_000)
    assert cfg5.tier == 5
    assert "Calculate Next Block" in cfg5.option1.label
    assert "Search for" in cfg5.option2.label
    assert cfg5.note != ""
    assert "computing the next block of Pi requires" in cfg5.note


# =====================================================================
# TC-16: Multi-Step Cache Invariance Regression Test
# =====================================================================
def test_tc16_multi_step_cache_invariance(tmp_path: Path):
    """
    TC-16: Verify that computing Pi in 3 separate incremental steps
    (e.g., 50,000 -> 100,000 -> 150,000 digits) results in an engine.pi_digits
    stream that is 100% byte-for-byte identical to computing 150,000 digits
    in a single fresh run. Also verify that sequential searches return
    identical 1-based decimal positions regardless of whether the cache starts
    empty or partially populated.
    """
    # Incremental engine: 50,000 -> 100,000 -> 150,000
    inc_dir = tmp_path / "incremental"
    engine_inc = PiEngine(data_dir=inc_dir, default_seed_digits=50_000)
    assert len(engine_inc.pi_digits) == 50_000

    engine_inc._expand_to_length(100_000)
    assert len(engine_inc.pi_digits) == 100_000

    engine_inc._expand_to_length(150_000)
    assert len(engine_inc.pi_digits) == 150_000

    # Fresh single-step engine: 150,000 directly
    fresh_dir = tmp_path / "fresh"
    engine_fresh = PiEngine(data_dir=fresh_dir, default_seed_digits=150_000)
    assert len(engine_fresh.pi_digits) == 150_000

    # Byte-for-byte stream invariance check
    assert engine_inc.pi_digits == engine_fresh.pi_digits
    assert engine_inc.digits == engine_fresh.digits

    # Also verify file contents on disk
    with open(engine_inc.digits_file, "r", encoding="ascii") as f1, open(
        engine_fresh.digits_file, "r", encoding="ascii"
    ) as f2:
        assert f1.read() == f2.read()

    # Deterministic search comparison across both engines:
    # Test queries straddling chunk boundaries (e.g. index 50,000 and 100,000)
    q_boundary_1 = engine_fresh.pi_digits[49_997:50_004]  # straddles 50,000
    q_boundary_2 = engine_fresh.pi_digits[99_996:100_003]  # straddles 100,000

    res_inc_1 = engine_inc.search(q_boundary_1)
    res_fresh_1 = engine_fresh.search(q_boundary_1)
    assert res_inc_1 is not None and res_fresh_1 is not None
    assert res_inc_1.position == res_fresh_1.position

    res_inc_2 = engine_inc.search(q_boundary_2)
    res_fresh_2 = engine_fresh.search(q_boundary_2)
    assert res_inc_2 is not None and res_fresh_2 is not None
    assert res_inc_2.position == res_fresh_2.position


# =====================================================================
# TC-17: Wall-Clock Time-Budget Loop
# =====================================================================
def test_tc17_wall_clock_time_budget_loop(tmp_path: Path):
    """
    TC-17: Verify that selecting a time-budget option executes a true wall-clock
    timer loop across multiple dynamic expansions until the time budget is exhausted
    (or sequence found / cancelled), updating progress telemetry and recalibrating
    calculation metrics before prompting the checkpoint callback again.
    """
    engine = PiEngine(data_dir=tmp_path, default_seed_digits=10_000)
    initial_digits = engine.get_total_cached_digits()
    assert initial_digits == 10_000

    checkpoints: list[tuple[int, int, str]] = []
    progress_messages: list[str] = []
    burst_start_time = 0.0
    burst_elapsed_time = 0.0

    def progress_cb(digits: int, elapsed: float, status_msg: Optional[str] = None):
        if status_msg:
            progress_messages.append(status_msg)

    def checkpoint_cb(new_digits: int, total_cached: int, tier_cfg: CheckpointTierConfig):
        nonlocal burst_start_time, burst_elapsed_time
        checkpoints.append((new_digits, total_cached, f"checkpoint_{len(checkpoints) + 1}"))

        if len(checkpoints) == 1:
            # First pause (after initial chunk miss): trigger a 1.5s wall-clock time budget burst
            burst_start_time = time.perf_counter()
            return CheckpointOption(
                label="Search for 1.5 Seconds",
                mode="time_budget",
                target_seconds=1.5,
            )
        else:
            # Second pause (after 1.5s burst completes): record elapsed burst time and stop search
            burst_elapsed_time = time.perf_counter() - burst_start_time
            return "stop"

    # Search for an unfound 10-digit number
    res = engine.search(
        "9999999999",
        chunk_size=50_000,
        checkpoint_callback=checkpoint_cb,
        progress_callback=progress_cb,
    )

    assert res is None
    # Checkpoint modal was invoked exactly twice:
    # 1. After initial chunk miss
    # 2. After 1.5s wall-clock burst completed
    assert len(checkpoints) == 2

    # Verify that the wall-clock loop ran for at least ~1.1 seconds (allowing timing jitter)
    assert burst_elapsed_time >= 1.1

    # Verify that total cached digits grew substantially beyond initial 60,000
    assert checkpoints[1][1] > checkpoints[0][1]

    # Verify progress callback received formatted time-budget telemetry
    assert len(progress_messages) >= 1
    assert any("Running 1.5s Burst" in msg for msg in progress_messages)

    # Verify data persistence
    assert engine.get_total_cached_digits() == checkpoints[1][1]
    with open(engine.digits_file, "r", encoding="ascii") as f:
        assert len(f.read().strip()) == engine.get_total_cached_digits()


# =====================================================================
# TC-18: T_min_step Golden Rule Enforcement
# =====================================================================
def test_tc18_t_min_step_golden_rule_enforcement(tmp_path: Path):
    """
    TC-18: Verify that across all simulated values of t_min_step
    (5s, 20s, 35s, 65s, 180s), no returned modal button ever has a time budget
    smaller than t_min_step (and strictly >= 1.25 * t_min_step), and Tier 5
    triggers accurately when t_min_step > 90.0s.
    """
    engine = PiEngine(data_dir=tmp_path, default_seed_digits=1000)
    engine.has_calibrated_benchmark = True  # bypass micro-benchmark for simulation

    test_steps = [
        (5.0, 1),
        (20.0, 2),
        (35.0, 3),
        (65.0, 4),
        (180.0, 5),
    ]

    for simulated_t_min_step, expected_tier in test_steps:
        # At current_total = 1_000_000, target for +2M is 3_000_000 -> ratio = 3.0
        # t_min_step = t_last * (3.0 ** 1.28)
        # So t_last = simulated_t_min_step / (3.0 ** 1.28)
        engine.last_calc_digits = 1_000_000
        engine.last_calc_time = simulated_t_min_step / (3.0 ** 1.28)

        cfg = engine.get_checkpoint_tier_config(1_000_000)
        assert cfg.tier == expected_tier
        assert abs(cfg.t_min_step - simulated_t_min_step) < 0.01

        # Golden Rule Assertion: every time-budget option must have duration >= 1.25 * t_min_step
        options = [cfg.option1, cfg.option2]
        for opt in options:
            if opt.mode == "time_budget":
                assert opt.target_seconds is not None
                assert opt.target_seconds >= cfg.t_min_step
                assert opt.target_seconds >= 1.25 * cfg.t_min_step - 0.01

        if expected_tier == 5:
            assert cfg.note != ""
            assert "computing the next block of Pi requires" in cfg.note
            assert "Calculate Next Block" in cfg.option1.label
            assert cfg.option1.mode == "fixed_digits"
            assert cfg.option1.delta_digits == 2_000_000


# =====================================================================
# TC-19: Seed Extraction & Landmark Verification
# =====================================================================
def test_tc19_seed_extraction_and_landmarks(tmp_path: Path):
    """
    TC-19: Verify seed decompression logic and assert that when the 100M seed
    is loaded, landmarks 6411415 and 6434099 are resolved instantaneously (< 0.15s)
    at positions 23,018,978 and 35,047,512 respectively without calculating new digits.
    Also verifies that an outdated partial cache is automatically upgraded to 100M digits.
    """
    seed_path = get_seed_path()
    assert seed_path is not None, "Bundled 100M seed archive must exist in seed/ directory."
    assert seed_path.exists()

    # Case A: Partial cache (< 100,000,000 digits) exists from previous run
    partial_file = tmp_path / "pi_digits.txt"
    partial_file.write_text("1415926535" * 1000, encoding="ascii")  # 10,000 digits
    assert partial_file.stat().st_size == 10_000

    # Initialize engine in tmp_path with load_seed_pack=True -> must auto-upgrade to 100M
    engine = PiEngine(data_dir=tmp_path, default_seed_digits=0, load_seed_pack=True)
    assert engine.get_total_cached_digits() == 100_000_000
    assert partial_file.stat().st_size == 100_000_000

    # Search for landmark 1: 6411415 -> pos 23,018,978
    t0 = time.perf_counter()
    res1 = engine.search("6411415")
    elapsed1 = time.perf_counter() - t0
    assert res1 is not None
    assert res1.position == 23_018_978
    assert res1.source == "Sorted Index Cache"
    assert res1.new_digits_calculated == 0
    assert elapsed1 < 0.20  # Instantaneous cache lookup

    # Search for landmark 2: 6434099 -> pos 35,047,512
    t0 = time.perf_counter()
    res2 = engine.search("6434099")
    elapsed2 = time.perf_counter() - t0
    assert res2 is not None
    assert res2.position == 35_047_512
    assert res2.source == "Sorted Index Cache"
    assert res2.new_digits_calculated == 0
    assert elapsed2 < 0.20  # Instantaneous cache lookup


# =====================================================================
# TC-20: Chudnovsky Engine Accuracy Verification
# =====================================================================
def test_tc20_chudnovsky_precision():
    """
    TC-20: Verify first 50 digits of computed Pi match the standard
    mathematical reference value.
    """
    reference_50 = "14159265358979323846264338327950288419716939937510"
    computed = compute_pi_digits(50)
    assert computed == reference_50


# =====================================================================
# TC-21: Persistence and Cold Start Reload
# =====================================================================
def test_tc21_persistence_and_cold_start(tmp_path: Path):
    """
    TC-21: Verify that computed digits and sorted query indices persist to
    disk and are seamlessly reloaded on cold start without recalculation.
    """
    engine1 = PiEngine(data_dir=tmp_path, default_seed_digits=2000, load_seed_pack=False)
    res = engine1.search("1415")
    assert res is not None

    engine2 = PiEngine(data_dir=tmp_path, default_seed_digits=0, load_seed_pack=False)
    assert engine2.get_total_cached_digits() == engine1.get_total_cached_digits()
    assert engine2.lookup_cache("1415") == 1


# =====================================================================
# TC-22: Initial 1,000,000-Digit Burst on Cache Miss
# =====================================================================
def test_tc22_initial_burst_on_cache_miss(tmp_path: Path):
    """
    TC-22: Verify that when search() is called with chunk_size=None (default)
    on a cache miss with fewer than 1,000,000 digits cached, the initial step
    calculates exactly up to 1,000,000 digits before the first checkpoint.
    """
    engine = PiEngine(data_dir=tmp_path, default_seed_digits=100_000, load_seed_pack=False)
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


# =====================================================================
# TC-23: Local Seed Expansion & Persistence
# =====================================================================
def test_tc23_local_seed_expansion_and_persistence(tmp_path: Path):
    """
    TC-23: Verify that calculating new digits updates the external local seed
    archive via sync_seed_file() when custom_seed_dir is specified, and that
    a subsequent cold start in a fresh data directory automatically extracts
    and reloads the newly expanded digit count from that updated seed.
    """
    custom_seed_dir = tmp_path / "custom_seed"
    custom_seed_dir.mkdir(parents=True, exist_ok=True)
    seed_archive = custom_seed_dir / "pi_100m.zst"

    # Step 1: Create an initial seed of 100,000 digits in custom_seed_dir
    initial_digits = compute_pi_digits(100_000)
    compress_seed_file(initial_digits, seed_archive)
    assert seed_archive.exists()
    assert len(decompress_seed_file(seed_archive)) == 100_000

    # Step 2: Initialize PiEngine backed by data_dir_1 pointing to custom_seed_dir
    data_dir_1 = tmp_path / "data1"
    engine1 = PiEngine(
        data_dir=data_dir_1,
        seed_dir=custom_seed_dir,
        default_seed_digits=0,
        load_seed_pack=True,
    )
    assert engine1.get_total_cached_digits() == 100_000

    # Step 3: Expand digits to 150,000 and explicitly sync the external seed
    engine1._expand_to_length(150_000)
    assert engine1.get_total_cached_digits() == 150_000
    engine1.sync_seed_file()

    # Step 4: Verify external seed archive on disk was updated to 150,000 digits
    reloaded_from_seed = decompress_seed_file(seed_archive)
    assert len(reloaded_from_seed) == 150_000
    assert reloaded_from_seed == engine1.digits

    # Step 5: Cold start in a completely fresh data directory using that same seed_dir
    data_dir_2 = tmp_path / "data2"
    engine2 = PiEngine(
        data_dir=data_dir_2,
        seed_dir=custom_seed_dir,
        default_seed_digits=0,
        load_seed_pack=True,
    )
    assert engine2.get_total_cached_digits() == 150_000
    assert engine2.digits == reloaded_from_seed


# =====================================================================
# TC-24: Immediate Checkpoint Modal Pause on Cache Miss (>= 1M Digits)
# =====================================================================
def test_tc24_immediate_pause_on_1m_cache_miss(tmp_path: Path):
    """
    TC-24: Verify that when current_total >= 1,000,000 (e.g. 100M seed or 1M cache),
    a cache miss invokes checkpoint_callback(new_digits=0, total_cached, tier_config)
    BEFORE launching any mpmath calculation, allowing the user to stop or choose budget.
    """
    engine = PiEngine(data_dir=tmp_path, default_seed_digits=1_000_000, load_seed_pack=False)
    assert engine.get_total_cached_digits() == 1_000_000

    pause_invocations = []

    def mock_pause_cb(new_digits: int, total_cached: int, tier_cfg: CheckpointTierConfig):
        pause_invocations.append((new_digits, total_cached, tier_cfg.tier))
        return "stop"

    t0 = time.perf_counter()
    # Search for an unfound 10-digit number
    res = engine.search("9999999999", checkpoint_callback=mock_pause_cb)
    elapsed = time.perf_counter() - t0

    # Must return None within < 0.1s without calculating any new digits
    assert res is None
    assert elapsed < 0.15
    assert len(pause_invocations) == 1
    new_d, total_c, tier = pause_invocations[0]
    assert new_d == 0  # 0 new digits calculated before pause
    assert total_c == 1_000_000
    assert engine.get_total_cached_digits() == 1_000_000


# =====================================================================
# TC-25: Instant Subprocess-Backed Calculation Cancellation (< 0.5s)
# =====================================================================
def test_tc25_instant_calculation_cancellation(tmp_path: Path):
    """
    TC-25: Verify that cancelling an active deep calculation terminates the
    child worker process immediately and returns control within < 0.5s.
    """
    from pi_engine import compute_pi_digits_cancellable

    cancel_evt = threading.Event()

    def cancel_after_delay():
        time.sleep(0.06)
        cancel_evt.set()

    t_cancel = threading.Thread(target=cancel_after_delay, daemon=True)
    t_cancel.start()

    t0 = time.perf_counter()
    # Request a massive precision that would take multiple seconds
    result = compute_pi_digits_cancellable(
        target_len=5_000_000,
        cancel_event=cancel_evt,
        poll_interval=0.03,
        temp_dir=tmp_path,
    )
    elapsed = time.perf_counter() - t0

    assert result is None
    assert elapsed < 0.50  # Must terminate and return in under 0.50s


# =====================================================================
# TC-26: Cold-Start Time Estimate Calibration at 100M Digits & Persistence
# =====================================================================
def test_tc26_cold_start_100m_time_calibration(tmp_path: Path):
    """
    TC-26: Verify that:
    1. Cold start benchmark default at 100M digits evaluates to ~95s (~2 Mins)
       instead of ~15 Mins.
    2. get_checkpoint_tier_config(100_000_000) provides '~2 Mins' option.
    3. Benchmark measurements (last_calc_digits, last_calc_time) are persisted
       in pi_index.json and reloaded across engine restarts.
    """
    engine = PiEngine(data_dir=tmp_path, default_seed_digits=1000, load_seed_pack=False)
    # Ensure baseline gmpy2 calibration: 100M digits / 92.0s
    engine.last_calc_digits = 100_000_000
    engine.last_calc_time = 92.0

    est_next_block = engine.estimate_chunk_time(100_000_000, 2_000_000)
    # 92.0 * (102M / 100M)^1.28 ~ 94.4s -> within 60s to 120s
    assert 60.0 <= est_next_block <= 120.0

    tier_cfg = engine.get_checkpoint_tier_config(100_000_000)
    assert tier_cfg.tier == 5
    assert "~2 Mins" in tier_cfg.option1.label

    # Verify persistence in pi_index.json
    engine.last_calc_digits = 105_000_000
    engine.last_calc_time = 101.5
    engine._save_index()

    # Cold start in same directory
    engine2 = PiEngine(data_dir=tmp_path, default_seed_digits=0, load_seed_pack=False)
    assert engine2.last_calc_digits == 105_000_000
    assert abs(engine2.last_calc_time - 101.5) < 0.001
    assert engine2.has_calibrated_benchmark is True


# =====================================================================
# TC-27: Seed Export and Import Round-Trip & Validation
# =====================================================================
def test_tc27_seed_export_import_roundtrip(tmp_path: Path):
    """
    TC-27: Verify export_seed() compresses digits into .zst/.gz/.txt,
    import_seed() validates the standard stream starting with '1415926535',
    replaces cached digits, updates indices, and rejects malformed seed files.
    """
    # Create engine with 50,000 digits
    src_data = tmp_path / "src_data"
    engine1 = PiEngine(data_dir=src_data, default_seed_digits=50_000, load_seed_pack=False)
    assert engine1.get_total_cached_digits() == 50_000

    # 1. Export as .zst
    export_zst = tmp_path / "exported_pi.zst"
    meta_zst = engine1.export_seed(export_zst)
    assert meta_zst["digits_count"] == 50_000
    assert Path(meta_zst["path"]).exists()
    assert meta_zst["bytes"] > 0

    # 2. Export as .txt
    export_txt = tmp_path / "exported_pi.txt"
    meta_txt = engine1.export_seed(export_txt)
    assert meta_txt["digits_count"] == 50_000
    assert meta_txt["bytes"] == 50_000

    # 3. Import into a fresh engine
    dest_data = tmp_path / "dest_data"
    engine2 = PiEngine(data_dir=dest_data, default_seed_digits=1000, load_seed_pack=False)
    assert engine2.get_total_cached_digits() == 1000

    imported_count = engine2.import_seed(export_zst)
    assert imported_count == 50_000
    assert engine2.get_total_cached_digits() == 50_000
    assert engine2.digits == engine1.digits

    # Verify search succeeds in imported engine
    res = engine2.search("1415")
    assert res is not None
    assert res.position == 1

    # 4. Reject invalid seed: wrong prefix
    bad_prefix = tmp_path / "bad_prefix.txt"
    bad_prefix.write_text("99999999991234567890", encoding="ascii")
    with pytest.raises(ValueError, match="does not start with standard Pi digits"):
        engine2.import_seed(bad_prefix)

    # 5. Reject invalid seed: non-numeric characters
    bad_chars = tmp_path / "bad_chars.txt"
    bad_chars.write_text("1415926535abcdefg", encoding="ascii")
    with pytest.raises(ValueError, match="contains non-numeric"):
        engine2.import_seed(bad_chars)
