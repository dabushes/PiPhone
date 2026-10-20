"""
PiBinaryIndex - Memory-Mapped 10-Digit Binary Search Index for PiPhone.
Specification:
- 16-Byte Header at offset 0:
    Bytes 0..4: Magic bytes b"PI10\\x01" (5 bytes)
    Byte 5: Index Mode (1 = STRAGGLER_SORTED_10B, 2 = FULL_DIRECT_5B)
    Bytes 6..10: cutoff_pos as 5-byte unsigned little-endian (uint40)
    Bytes 11..15: max_pi_digit_scanned as 5-byte unsigned little-endian (uint40)
- Mode 1 (STRAGGLER_SORTED_10B):
    Stores sorted pairs of [phone_int: uint40][pi_start_pos: uint40] (10 bytes/record).
    Lookup uses Python mmap binary search (O(log N) disk reads, near-zero RAM).
- Mode 2 (FULL_DIRECT_5B):
    Direct-address table where byte_offset = 16 + (phone_int * 5).
    Reads 5 bytes uint40. A value of 0 indicates not found; > 0 is 1-based start pos.
"""

from __future__ import annotations

import mmap
import os
from enum import IntEnum
from pathlib import Path
from typing import Optional, Tuple

MAGIC_BYTES = b"PI10\x01"
HEADER_SIZE = 16
RECORD_SIZE_STRAGGLER = 10
RECORD_SIZE_DIRECT = 5
MAX_PHONE_INT = 9_999_999_999


class BinaryIndexMode(IntEnum):
    STRAGGLER_SORTED_10B = 1
    FULL_DIRECT_5B = 2


def encode_header(
    mode: BinaryIndexMode | int,
    cutoff_pos: int,
    max_pi_digit_scanned: int,
) -> bytes:
    """Encodes the 16-byte .piidx file header."""
    mode_int = int(mode)
    if mode_int not in (BinaryIndexMode.STRAGGLER_SORTED_10B, BinaryIndexMode.FULL_DIRECT_5B):
        raise ValueError(f"Invalid BinaryIndexMode: {mode_int}")
    
    header = bytearray(HEADER_SIZE)
    header[0:5] = MAGIC_BYTES
    header[5] = mode_int
    header[6:11] = int(cutoff_pos).to_bytes(5, byteorder="little", signed=False)
    header[11:16] = int(max_pi_digit_scanned).to_bytes(5, byteorder="little", signed=False)
    return bytes(header)


def decode_header(header_bytes: bytes) -> Tuple[BinaryIndexMode, int, int]:
    """Decodes the 16-byte .piidx file header. Returns (mode, cutoff_pos, max_pi_digit_scanned)."""
    if len(header_bytes) < HEADER_SIZE:
        raise ValueError(f"Header data too short: {len(header_bytes)} bytes < {HEADER_SIZE} bytes")
    
    magic = header_bytes[0:5]
    if magic != MAGIC_BYTES:
        raise ValueError(f"Invalid index magic bytes: {magic!r}, expected {MAGIC_BYTES!r}")
    
    mode_val = header_bytes[5]
    if mode_val not in (BinaryIndexMode.STRAGGLER_SORTED_10B, BinaryIndexMode.FULL_DIRECT_5B):
        raise ValueError(f"Unsupported binary index mode: {mode_val}")
    
    mode = BinaryIndexMode(mode_val)
    cutoff_pos = int.from_bytes(header_bytes[6:11], byteorder="little", signed=False)
    max_scanned = int.from_bytes(header_bytes[11:16], byteorder="little", signed=False)
    return mode, cutoff_pos, max_scanned


def format_latency(seconds: float) -> str:
    """Formats high-resolution execution latency in µs, ms, or s."""
    if seconds < 0.001:
        micros = seconds * 1_000_000.0
        return f"{micros:.1f} µs"
    elif seconds < 1.0:
        millis = seconds * 1_000.0
        return f"{millis:.2f} ms"
    else:
        return f"{seconds:.2f} s"


class PiBinaryIndex:
    """
    Memory-mapped reader for 10-digit .piidx binary index files.
    Allows ultra-fast (sub-millisecond) lookups of 10-digit phone numbers in Pi.
    """

    def __init__(self, index_path: str | Path):
        self.path = Path(index_path).resolve()
        self.file = None
        self.mmap: Optional[mmap.mmap] = None
        self.mode: BinaryIndexMode = BinaryIndexMode.STRAGGLER_SORTED_10B
        self.cutoff_pos: int = 0
        self.max_pi_digit_scanned: int = 0
        self.record_count: int = 0
        self.file_size: int = 0
        self._open()

    def _open(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"Binary index file not found: {self.path}")
        
        self.file_size = self.path.stat().st_size
        if self.file_size < HEADER_SIZE:
            raise ValueError(
                f"Corrupt index file: size ({self.file_size} bytes) < header size ({HEADER_SIZE} bytes)"
            )

        self.file = open(self.path, "rb")
        try:
            self.mmap = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)
        except Exception:
            self.file.close()
            self.file = None
            raise

        mode, cutoff, max_scanned = decode_header(self.mmap[0:HEADER_SIZE])
        self.mode = mode
        self.cutoff_pos = cutoff
        self.max_pi_digit_scanned = max_scanned

        if self.mode == BinaryIndexMode.STRAGGLER_SORTED_10B:
            self.record_count = (self.file_size - HEADER_SIZE) // RECORD_SIZE_STRAGGLER
        else:
            self.record_count = 10_000_000_000

    def lookup(self, phone_str: str) -> Optional[int]:
        """
        Looks up the exact 1-based starting position in Pi for a 10-digit number.
        Returns None if not present in the index.
        """
        if self.mmap is None:
            return None
        
        if len(phone_str) != 10 or not phone_str.isdigit():
            return None

        target_int = int(phone_str)
        if target_int > MAX_PHONE_INT:
            return None

        if self.mode == BinaryIndexMode.STRAGGLER_SORTED_10B:
            low = 0
            high = self.record_count - 1
            mm = self.mmap

            while low <= high:
                mid = (low + high) // 2
                offset = HEADER_SIZE + (mid * RECORD_SIZE_STRAGGLER)
                rec_phone = int.from_bytes(mm[offset : offset + 5], byteorder="little", signed=False)

                if rec_phone == target_int:
                    pi_pos = int.from_bytes(
                        mm[offset + 5 : offset + 10], byteorder="little", signed=False
                    )
                    return pi_pos if pi_pos > 0 else None
                elif rec_phone < target_int:
                    low = mid + 1
                else:
                    high = mid - 1
            return None

        elif self.mode == BinaryIndexMode.FULL_DIRECT_5B:
            offset = HEADER_SIZE + (target_int * RECORD_SIZE_DIRECT)
            if offset + RECORD_SIZE_DIRECT > self.file_size:
                return None
            pi_pos = int.from_bytes(
                self.mmap[offset : offset + RECORD_SIZE_DIRECT],
                byteorder="little",
                signed=False,
            )
            return pi_pos if pi_pos > 0 else None

        return None

    def close(self) -> None:
        """Closes memory-mapped file handles cleanly."""
        if self.mmap is not None:
            try:
                self.mmap.close()
            except Exception:
                pass
            self.mmap = None
        if self.file is not None:
            try:
                self.file.close()
            except Exception:
                pass
            self.file = None

    def __enter__(self) -> "PiBinaryIndex":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __repr__(self) -> str:
        mode_str = "STRAGGLER" if self.mode == BinaryIndexMode.STRAGGLER_SORTED_10B else "FULL_DIRECT"
        return (
            f"<PiBinaryIndex path={self.path.name} mode={mode_str} "
            f"cutoff={self.cutoff_pos:,} max_scanned={self.max_pi_digit_scanned:,} "
            f"records={self.record_count:,}>"
        )

