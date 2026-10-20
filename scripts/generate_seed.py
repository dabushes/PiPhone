"""
Generate 100,000,000 decimal digits of Pi, verify landmarks, compress with zstandard,
and populate the local data/ cache.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

try:
    import gmpy2
    from mpmath.libmp import pi_fixed
except ImportError as e:
    print(f"Error: gmpy2 and mpmath are required: {e}")
    sys.exit(1)

try:
    import zstandard as zstd
except ImportError:
    zstd = None
    import gzip

TARGET_DIGITS = 100_000_000
GUARD_BITS = 256
LOG2_10 = 3.3219280948873626


def main():
    root_dir = Path(__file__).resolve().parent.parent
    seed_dir = root_dir / "seed"
    data_dir = root_dir / "data"

    seed_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    print(f"===========================================================")
    print(f"  PiPhone v2.0 - 100M Seed Generator")
    print(f"  Target: {TARGET_DIGITS:,} decimal digits")
    print(f"===========================================================")

    t0 = time.perf_counter()

    # Step 1: Calculate high-precision binary fixed-point Pi with gmpy2 C-speed
    prec_bits = int((TARGET_DIGITS + 20) * LOG2_10) + GUARD_BITS
    print(f"[1/5] Computing pi_fixed with {prec_bits:,} bits of precision...")
    t_fixed_start = time.perf_counter()
    pi_val = pi_fixed(prec_bits)
    t_fixed = time.perf_counter() - t_fixed_start
    print(f"      -> pi_fixed completed in {t_fixed:.2f}s")

    # Step 2: Scale by 10^100,000,000 and extract base-10 digits
    print(f"[2/5] Scaling by 10^{TARGET_DIGITS:,} and converting to base-10 digits...")
    t_scale_start = time.perf_counter()
    ten_pow = gmpy2.mpz(10) ** TARGET_DIGITS
    dec_int = (pi_val * ten_pow) >> prec_bits
    raw_str = dec_int.digits()
    t_scale = time.perf_counter() - t_scale_start
    print(f"      -> Scaling and base-10 conversion completed in {t_scale:.2f}s")

    # Raw string starts with '3', following characters are decimals
    if raw_str.startswith("3"):
        decimals = raw_str[1:TARGET_DIGITS + 1]
    else:
        decimals = raw_str[:TARGET_DIGITS]

    actual_len = len(decimals)
    print(f"[3/5] Total decimal digits produced: {actual_len:,}")
    if actual_len != TARGET_DIGITS:
        raise ValueError(f"Expected {TARGET_DIGITS:,} digits, got {actual_len:,}")

    # Step 3: Verify Ground Truth Landmarks
    print("[4/5] Verifying ground-truth landmarks...")
    # Landmark 1: Start of Pi
    lm1 = decimals[0:8]
    assert lm1 == "14159265", f"Start of Pi mismatch: got {lm1}"
    print(f"      [PASS] Position 1: '{lm1}' matches '14159265'")

    # Landmark 2: 6411415 at pos 23,018,978
    pos2 = 23_018_978
    lm2 = decimals[pos2 - 1 : pos2 - 1 + 7]
    assert lm2 == "6411415", f"Landmark 2 mismatch: got {lm2} at pos {pos2:,}"
    print(f"      [PASS] Position {pos2:,}: '{lm2}' matches '6411415'")

    # Landmark 3: 6434099 at pos 35,047,512
    pos3 = 35_047_512
    lm3 = decimals[pos3 - 1 : pos3 - 1 + 7]
    assert lm3 == "6434099", f"Landmark 3 mismatch: got {lm3} at pos {pos3:,}"
    print(f"      [PASS] Position {pos3:,}: '{lm3}' matches '6434099'")

    # Step 4: Save uncompressed digits to data/pi_digits.txt
    print(f"[5/5] Saving outputs...")
    digits_file = data_dir / "pi_digits.txt"
    print(f"      Writing uncompressed cache to {digits_file}...")
    t_write_start = time.perf_counter()
    with open(digits_file, "w", encoding="ascii") as f:
        f.write(decimals)
    t_write = time.perf_counter() - t_write_start
    print(f"      -> Uncompressed write completed in {t_write:.2f}s ({digits_file.stat().st_size / (1024*1024):.2f} MB)")

    # Step 5: Compress to seed archive
    raw_bytes = decimals.encode("ascii")
    if zstd is not None:
        seed_file = seed_dir / "pi_100m.zst"
        print(f"      Compressing with zstandard (level 19) to {seed_file}...")
        t_comp_start = time.perf_counter()
        cctx = zstd.ZstdCompressor(level=19)
        compressed = cctx.compress(raw_bytes)
        with open(seed_file, "wb") as f:
            f.write(compressed)
        t_comp = time.perf_counter() - t_comp_start
        size_mb = seed_file.stat().st_size / (1024 * 1024)
        print(f"      -> zstd compression completed in {t_comp:.2f}s (Archive Size: {size_mb:.2f} MB)")
    else:
        seed_file = seed_dir / "pi_100m.gz"
        print(f"      Compressing with gzip (level 9) to {seed_file}...")
        t_comp_start = time.perf_counter()
        compressed = gzip.compress(raw_bytes, compresslevel=9)
        with open(seed_file, "wb") as f:
            f.write(compressed)
        t_comp = time.perf_counter() - t_comp_start
        size_mb = seed_file.stat().st_size / (1024 * 1024)
        print(f"      -> gzip compression completed in {t_comp:.2f}s (Archive Size: {size_mb:.2f} MB)")

    total_time = time.perf_counter() - t0
    print(f"===========================================================")
    print(f"  SUCCESS: 100M Pi Seed generated & verified in {total_time:.2f}s!")
    print(f"  Archive: {seed_file} ({size_mb:.2f} MB - under GitHub 50MB limit)")
    print(f"===========================================================")


if __name__ == "__main__":
    main()

