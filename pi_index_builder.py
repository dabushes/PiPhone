"""
PiIndexBuilder - High-Performance 10-Digit Binary Index Builder for PiPhone.
Features:
- 1.25 GB Bitset Tracking (10,000,000,000 bits) tracking first occurrence of 0000000000..9999999999.
- 9-Digit Boundary Stitch across streaming chunks with O(1) rolling integer window.
- Universal stream input: local in-memory Pi seed, raw .txt, compressed .zst, or directory chunks.
- Dual-format output: Mode 1 (STRAGGLER_SORTED_10B) or Mode 2 (FULL_DIRECT_5B).
- Atomic checkpoint save/resume (bitset.ckpt.bin + state.ckpt.json + .unsorted.tmp truncation).
- Accessible via CLI and PiPhone GUI Index Manager.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Generator, Iterable, Optional

from pi_binary_index import (
    HEADER_SIZE,
    MAGIC_BYTES,
    RECORD_SIZE_DIRECT,
    RECORD_SIZE_STRAGGLER,
    BinaryIndexMode,
    encode_header,
)

DEFAULT_CHUNK_SIZE = 32 * 1024 * 1024  # 32 MB chunks for streaming


class BinaryIndexBuilder:
    """
    Builds a 10-digit binary index (.piidx) from a stream of Pi digits.
    """

    def __init__(
        self,
        mode: BinaryIndexMode | int = BinaryIndexMode.STRAGGLER_SORTED_10B,
        cutoff_pos: int = 0,
        output_path: Optional[Path | str] = None,
        checkpoint_dir: Optional[Path | str] = None,
        bitset_bytes: int = 1_250_000_000,
    ):
        self.mode = BinaryIndexMode(int(mode))
        self.cutoff_pos = int(cutoff_pos)
        self.output_path = Path(output_path).resolve() if output_path else None
        self.checkpoint_dir = Path(checkpoint_dir).resolve() if checkpoint_dir else None
        
        # Allocate 1.25 GB bitset (1 bit per 10-digit number from 0 to 9,999,999,999)
        self.bitset_size = bitset_bytes
        self.bitset: bytearray | Any = bytearray(self.bitset_size)

        self.window: int = 0
        self.current_pos: int = 0
        self.unique_count: int = 0
        self.tail_9: str = ""

        # File handles for building
        self._unsorted_file = None
        self._unsorted_path: Optional[Path] = None
        self._direct_file = None

    def _init_build_files(self) -> None:
        if self.output_path is None:
            raise ValueError("output_path must be specified before building.")

        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        if self.mode == BinaryIndexMode.STRAGGLER_SORTED_10B:
            self._unsorted_path = self.output_path.parent / f"{self.output_path.name}.unsorted.tmp"
            self._unsorted_file = open(self._unsorted_path, "a+b")
        elif self.mode == BinaryIndexMode.FULL_DIRECT_5B:
            # Initialize direct file with header if new
            if not self.output_path.exists() or self.output_path.stat().st_size < HEADER_SIZE:
                with open(self.output_path, "wb") as f:
                    header = encode_header(self.mode, self.cutoff_pos, 0)
                    f.write(header)
            self._direct_file = open(self.output_path, "r+b")

    def process_digits(self, digits_chunk: str) -> int:
        """
        Processes a string of decimal digits, carrying over the 9-digit boundary stitch.
        Returns the number of digits processed.
        """
        if not digits_chunk:
            return 0

        # Strip any leading '3.' if at start of stream
        if self.current_pos == 0 and digits_chunk.startswith("3."):
            digits_chunk = digits_chunk[2:]
        elif self.current_pos == 0 and digits_chunk.startswith("3"):
            # Check if followed by 1415
            if digits_chunk.startswith("31415"):
                digits_chunk = digits_chunk[1:]

        # Filter out non-digits (such as newlines or whitespace)
        cleaned = "".join(c for c in digits_chunk if "0" <= c <= "9")
        if not cleaned:
            return 0

        # Prepend tail_9 from previous chunk to maintain rolling 10-digit window across boundaries
        stream = self.tail_9 + cleaned
        start_idx = len(self.tail_9)
        self.tail_9 = stream[-9:] if len(stream) >= 9 else stream

        bitset = self.bitset
        mode = self.mode
        cutoff = self.cutoff_pos
        unsorted_f = self._unsorted_file
        direct_f = self._direct_file

        w = self.window
        pos = self.current_pos
        u_count = self.unique_count

        # Fast inner loop
        for i in range(start_idx, len(stream)):
            digit = ord(stream[i]) - 48
            w = ((w % 1_000_000_000) * 10) + digit
            pos += 1

            if pos >= 10:
                pi_start_pos = pos - 9
                b_idx = w >> 3
                mask = 1 << (w & 7)

                if not (bitset[b_idx] & mask):
                    bitset[b_idx] |= mask
                    u_count += 1

                    if mode == BinaryIndexMode.STRAGGLER_SORTED_10B:
                        if pi_start_pos > cutoff and unsorted_f is not None:
                            rec = w.to_bytes(5, "little") + pi_start_pos.to_bytes(5, "little")
                            unsorted_f.write(rec)
                    elif mode == BinaryIndexMode.FULL_DIRECT_5B and direct_f is not None:
                        direct_f.seek(HEADER_SIZE + (w * RECORD_SIZE_DIRECT))
                        direct_f.write(pi_start_pos.to_bytes(5, "little"))

        self.window = w
        self.current_pos = pos
        self.unique_count = u_count
        return len(cleaned)

    def finalize(self) -> Path:
        """
        Finalizes index creation:
        - In STRAGGLER mode: sorts records by phone_int, prepends header, and writes .piidx.
        - In FULL_DIRECT mode: updates header with max_scanned and flushes file.
        Returns final Path to the .piidx file.
        """
        if self.output_path is None:
            raise ValueError("output_path is not defined.")

        if self.mode == BinaryIndexMode.STRAGGLER_SORTED_10B:
            if self._unsorted_file is not None:
                self._unsorted_file.flush()
                self._unsorted_file.close()
                self._unsorted_file = None

            unsorted_path = self._unsorted_path or (
                self.output_path.parent / f"{self.output_path.name}.unsorted.tmp"
            )

            record_count = 0
            if unsorted_path.exists():
                file_size = unsorted_path.stat().st_size
                record_count = file_size // RECORD_SIZE_STRAGGLER

            header = encode_header(self.mode, self.cutoff_pos, self.current_pos)

            if record_count == 0:
                with open(self.output_path, "wb") as f:
                    f.write(header)
                if unsorted_path.exists():
                    try:
                        unsorted_path.unlink()
                    except Exception:
                        pass
                return self.output_path

            # Sort records by phone_int (first 5 bytes of each 10-byte record)
            # For moderate file sizes (<= 250MB / 25M records), sort in RAM
            max_in_memory_records = 25_000_000
            if record_count <= max_in_memory_records:
                records = []
                with open(unsorted_path, "rb") as f_in:
                    while True:
                        chunk = f_in.read(10_000_000 * RECORD_SIZE_STRAGGLER)
                        if not chunk:
                            break
                        for i in range(0, len(chunk), RECORD_SIZE_STRAGGLER):
                            rec = chunk[i : i + RECORD_SIZE_STRAGGLER]
                            if len(rec) == RECORD_SIZE_STRAGGLER:
                                p_int = int.from_bytes(rec[:5], "little")
                                pi_pos = int.from_bytes(rec[5:], "little")
                                records.append((p_int, pi_pos))

                records.sort(key=lambda r: r[0])

                with open(self.output_path, "wb") as f_out:
                    f_out.write(header)
                    batch = bytearray()
                    for p_int, pi_pos in records:
                        batch += p_int.to_bytes(5, "little") + pi_pos.to_bytes(5, "little")
                        if len(batch) >= 1_000_000 * RECORD_SIZE_STRAGGLER:
                            f_out.write(batch)
                            batch = bytearray()
                    if batch:
                        f_out.write(batch)
            else:
                # Chunked external sort
                self._external_sort_records(unsorted_path, self.output_path, header)

            if unsorted_path.exists():
                try:
                    unsorted_path.unlink()
                except Exception:
                    pass

            if "records" in locals():
                del records

        elif self.mode == BinaryIndexMode.FULL_DIRECT_5B:
            if self._direct_file is not None:
                self._direct_file.seek(0)
                header = encode_header(self.mode, self.cutoff_pos, self.current_pos)
                self._direct_file.write(header)
                self._direct_file.flush()
                self._direct_file.close()
                self._direct_file = None

        # Clean up in-memory bitset buffer and trigger garbage collection
        self.bitset = bytearray(0)
        gc.collect()

        return self.output_path

    def _external_sort_records(
        self,
        src_path: Path,
        dst_path: Path,
        header: bytes,
        chunk_records: int = 10_000_000,
    ) -> None:
        """Performs external k-way merge sort for massive unsorted record files."""
        import heapq
        import tempfile

        temp_runs = []
        try:
            with open(src_path, "rb") as f_in:
                run_idx = 0
                while True:
                    raw = f_in.read(chunk_records * RECORD_SIZE_STRAGGLER)
                    if not raw:
                        break
                    records = []
                    for i in range(0, len(raw), RECORD_SIZE_STRAGGLER):
                        rec = raw[i : i + RECORD_SIZE_STRAGGLER]
                        if len(rec) == RECORD_SIZE_STRAGGLER:
                            records.append((int.from_bytes(rec[:5], "little"), int.from_bytes(rec[5:], "little")))
                    records.sort(key=lambda r: r[0])

                    run_file = src_path.parent / f"{src_path.name}.run_{run_idx}.tmp"
                    with open(run_file, "wb") as f_run:
                        for p_int, pi_pos in records:
                            f_run.write(p_int.to_bytes(5, "little") + pi_pos.to_bytes(5, "little"))
                    temp_runs.append(run_file)
                    run_idx += 1

            # K-way merge using heapq
            def record_generator(run_f):
                while True:
                    b = run_f.read(RECORD_SIZE_STRAGGLER)
                    if len(b) < RECORD_SIZE_STRAGGLER:
                        break
                    yield (int.from_bytes(b[:5], "little"), int.from_bytes(b[5:], "little"))

            opened_runs = [open(rf, "rb") for rf in temp_runs]
            generators = [record_generator(f) for f in opened_runs]

            with open(dst_path, "wb") as f_out:
                f_out.write(header)
                out_batch = bytearray()
                for p_int, pi_pos in heapq.merge(*generators, key=lambda r: r[0]):
                    out_batch += p_int.to_bytes(5, "little") + pi_pos.to_bytes(5, "little")
                    if len(out_batch) >= 1_000_000 * RECORD_SIZE_STRAGGLER:
                        f_out.write(out_batch)
                        out_batch = bytearray()
                if out_batch:
                    f_out.write(out_batch)

            for f in opened_runs:
                f.close()
        finally:
            for rf in temp_runs:
                if rf.exists():
                    try:
                        rf.unlink()
                    except Exception:
                        pass
            gc.collect()

    def save_checkpoint(self, ckpt_dir: Path | str) -> None:
        """Saves bitset and builder state for atomic pause/resume."""
        c_dir = Path(ckpt_dir).resolve()
        c_dir.mkdir(parents=True, exist_ok=True)

        if self._unsorted_file is not None:
            self._unsorted_file.flush()
        if self._direct_file is not None:
            self._direct_file.flush()

        unsorted_bytes = 0
        if self._unsorted_path and self._unsorted_path.exists():
            unsorted_bytes = self._unsorted_path.stat().st_size

        state = {
            "mode": int(self.mode),
            "cutoff_pos": self.cutoff_pos,
            "max_pi_digit_scanned": self.current_pos,
            "window": self.window,
            "tail_9": self.tail_9,
            "unique_count": self.unique_count,
            "unsorted_bytes": unsorted_bytes,
        }

        with open(c_dir / "state.ckpt.json", "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

        with open(c_dir / "bitset.ckpt.bin", "wb") as f:
            f.write(self.bitset)

    def load_checkpoint(self, ckpt_dir: Path | str) -> None:
        """Resumes builder state from a saved checkpoint."""
        c_dir = Path(ckpt_dir).resolve()
        state_file = c_dir / "state.ckpt.json"
        bitset_file = c_dir / "bitset.ckpt.bin"

        if not state_file.exists() or not bitset_file.exists():
            raise FileNotFoundError(f"Checkpoint files missing in {c_dir}")

        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)

        self.mode = BinaryIndexMode(state["mode"])
        self.cutoff_pos = state["cutoff_pos"]
        self.current_pos = state["max_pi_digit_scanned"]
        self.window = state["window"]
        self.tail_9 = state["tail_9"]
        self.unique_count = state["unique_count"]

        with open(bitset_file, "rb") as f:
            self.bitset = bytearray(f.read())

        # Truncate unsorted file to checkpointed boundary if it exists
        if self._unsorted_file is not None and "unsorted_bytes" in state:
            self._unsorted_file.truncate(state["unsorted_bytes"])
            self._unsorted_file.seek(state["unsorted_bytes"])

    def build_from_string(
        self,
        digits: str,
        progress_callback: Optional[Callable[[int, int, float], None]] = None,
        cancel_event: Optional[Any] = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> Path:
        """Builds index from an in-memory string of Pi digits (e.g. PiEngine local seed)."""
        self._init_build_files()
        total_len = len(digits)
        start_time = time.perf_counter()

        for i in range(0, total_len, chunk_size):
            if cancel_event is not None and cancel_event.is_set():
                break
            chunk = digits[i : i + chunk_size]
            self.process_digits(chunk)

            if progress_callback is not None:
                elapsed = max(0.001, time.perf_counter() - start_time)
                rate = self.current_pos / elapsed
                progress_callback(self.current_pos, self.unique_count, rate)

        return self.finalize()

    def build_from_file(
        self,
        input_path: Path | str,
        progress_callback: Optional[Callable[[int, int, float], None]] = None,
        cancel_event: Optional[Any] = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> Path:
        """
        Builds index from a file (.txt, .zst, .gz) or folder containing Pi digits.
        """
        self._init_build_files()
        in_p = Path(input_path).resolve()
        if not in_p.exists():
            raise FileNotFoundError(f"Input path not found: {in_p}")

        start_time = time.perf_counter()

        def handle_chunk(chunk_str: str):
            self.process_digits(chunk_str)
            if progress_callback is not None:
                elapsed = max(0.001, time.perf_counter() - start_time)
                rate = self.current_pos / elapsed
                progress_callback(self.current_pos, self.unique_count, rate)

        if in_p.is_dir():
            # Directory of chunk files (sorted alphanumerically)
            files = sorted(in_p.glob("*"))
            for f in files:
                if f.is_file():
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    self._read_and_process_file(f, handle_chunk, chunk_size, cancel_event)
        else:
            self._read_and_process_file(in_p, handle_chunk, chunk_size, cancel_event)

        return self.finalize()

    def _read_and_process_file(
        self,
        file_path: Path,
        chunk_handler: Callable[[str], None],
        chunk_size: int,
        cancel_event: Optional[Any],
    ) -> None:
        name_lower = file_path.name.lower()

        if name_lower.endswith(".zst"):
            try:
                import zstandard as zstd
                dctx = zstd.ZstdDecompressor()
                with open(file_path, "rb") as fh:
                    with dctx.stream_reader(fh) as reader:
                        while True:
                            if cancel_event is not None and cancel_event.is_set():
                                break
                            chunk = reader.read(chunk_size)
                            if not chunk:
                                break
                            chunk_handler(chunk.decode("ascii", errors="ignore"))
                return
            except ImportError:
                pass

        if name_lower.endswith(".gz"):
            import gzip
            with gzip.open(file_path, "rt", encoding="ascii", errors="ignore") as gz_f:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    chunk = gz_f.read(chunk_size)
                    if not chunk:
                        break
                    chunk_handler(chunk)
            return

        # Plain text or y-cruncher raw digit file
        with open(file_path, "r", encoding="ascii", errors="ignore") as f:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    break
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                chunk_handler(chunk)


def main():
    parser = argparse.ArgumentParser(
        description="PiIndexBuilder - CLI tool for building 10-digit .piidx binary lookup indexes."
    )
    parser.add_argument("-i", "--input", help="Path to input Pi digits file (.txt, .zst, .gz) or directory")
    parser.add_argument("--local-seed", action="store_true", help="Build index from local PiPhone data/pi_digits.txt")
    parser.add_argument("-o", "--output", required=True, help="Destination path for .piidx index file")
    parser.add_argument(
        "-m", "--mode",
        choices=["straggler", "full_direct"],
        default="straggler",
        help="Index Mode: 'straggler' (sorted 10B records) or 'full_direct' (5B direct array)",
    )
    parser.add_argument(
        "-c", "--cutoff",
        type=int,
        default=0,
        help="Cutoff position for straggler mode (default: 0)",
    )
    parser.add_argument("--resume", help="Directory containing checkpoint to resume from")
    parser.add_argument("--chunk-size", type=int, default=32_000_000, help="Chunk size in bytes (default: 32MB)")

    args = parser.parse_args()

    mode_enum = (
        BinaryIndexMode.FULL_DIRECT_5B if args.mode == "full_direct" else BinaryIndexMode.STRAGGLER_SORTED_10B
    )

    builder = BinaryIndexBuilder(
        mode=mode_enum,
        cutoff_pos=args.cutoff,
        output_path=args.output,
    )

    if args.resume:
        print(f"[BUILDER] Resuming build from checkpoint: {args.resume}")
        builder.load_checkpoint(args.resume)

    def progress(scanned: int, unique: int, rate: float):
        print(
            f"\r[BUILDER] Scanned {scanned:,} digits | Unique 10-digits: {unique:,} | Speed: {rate/1_000_000:.2f} M digits/s",
            end="",
            flush=True,
        )

    t0 = time.perf_counter()

    if args.local_seed:
        # Resolve data/pi_digits.txt relative to this script
        from pi_engine import get_app_dir
        seed_file = get_app_dir() / "data" / "pi_digits.txt"
        if not seed_file.exists():
            print(f"[ERROR] Local seed file not found at: {seed_file}")
            sys.exit(1)
        print(f"[BUILDER] Building index from local seed: {seed_file}")
        out = builder.build_from_file(seed_file, progress_callback=progress, chunk_size=args.chunk_size)
    elif args.input:
        print(f"[BUILDER] Building index from: {args.input}")
        out = builder.build_from_file(args.input, progress_callback=progress, chunk_size=args.chunk_size)
    else:
        print("[ERROR] Please specify either --input <file> or --local-seed")
        sys.exit(1)

    elapsed = time.perf_counter() - t0
    print(f"\n[BUILDER] Complete! Built index in {elapsed:.2f}s -> {out}")
    print(f"[BUILDER] Scanned: {builder.current_pos:,} digits | Indexed: {builder.unique_count:,} unique numbers")


if __name__ == "__main__":
    main()

