"""
PiPhone - Interactive Dark-Mode Pi Dialer & Decimal Expansion Search Engine
Version 2.4: Seed Import Staging, Strict ASCII-Digit Validation, PyInstaller Spec Alignment,
and Failure-Mode Test Suite.
"""

from __future__ import annotations

import gc
import json
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from typing import Any, Callable, Optional

# Explicit import for PyInstaller bundling compatibility on Python 3.12+ and C-extensions
try:
    import _pylong
except ImportError:
    pass

try:
    import gmpy2
except ImportError:
    pass

import customtkinter as ctk

from pi_binary_index import (
    BinaryIndexMode,
    PiBinaryIndex,
    format_latency,
)
from pi_engine import (
    CheckpointTierConfig,
    PiEngine,
    SearchEngineMode,
    SearchResult,
    format_context,
    format_delta_digits,
    validate_query,
)

# Configure global visual appearance
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class CheckpointModal(ctk.CTkToplevel):
    """
    Modal dialog displayed when an ongoing search calculation exceeds
    the cached digits and pauses at a checkpoint boundary.
    Configures buttons dynamically based on the 4-tier escalating schedule.
    Blocks interaction with the main window until the user selects an action.
    """

    def __init__(
        self,
        parent: ctk.CTk,
        new_digits: int,
        total_cached: int,
        tier_config: CheckpointTierConfig,
        callback: Callable[[str | int], None],
        query: str = "",
    ):
        super().__init__(parent)
        self.title("Search Checkpoint")
        self.geometry("420x330")
        self.resizable(False, False)
        self.callback = callback
        self._action_taken = False

        # Make modal window blocking
        self.transient(parent)
        self.grab_set()

        # Center over parent window
        self.update_idletasks()
        try:
            x = parent.winfo_x() + (parent.winfo_width() // 2) - 210
            y = parent.winfo_y() + (parent.winfo_height() // 2) - 165
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Telemetry & prompt description
        if new_digits == 0:
            q_label = f"'{query}'" if query else "The entered sequence"
            msg = (
                f"Sequence {q_label} was not found in the first {total_cached:,} cached digits of Pi.\n\n"
                f"Expanding Pi beyond {total_cached:,} digits requires heavy computation. "
                f"Would you like to calculate deeper?\n"
            )
            if tier_config.note:
                msg += f"\n{tier_config.note}\n"
            msg += "\nSelect calculation budget:"
            self.geometry("440x360")
        else:
            msg = (
                f"Match not found yet.\n\n"
                f"Calculated {new_digits:,} new digits\n"
                f"(Total Pi Digits Cached: {total_cached:,}).\n\n"
                f"Speed Tier {tier_config.tier} (Min Step est: {PiEngine.format_estimated_time(tier_config.t_min_step)})\n"
            )
            if tier_config.note:
                msg += f"\n{tier_config.note}\n\nSelect next calculation budget:"
                self.geometry("440x360")
            else:
                msg += "Select next calculation budget:"
                self.geometry("420x310")

        self.lbl_msg = ctk.CTkLabel(
            self,
            text=msg,
            font=ctk.CTkFont(size=13),
            justify="center",
            wraplength=380,
        )
        self.lbl_msg.pack(padx=16, pady=(16, 10))

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=24, pady=(0, 16))

        # Option 1: Configured by Tier
        self.btn_opt1 = ctk.CTkButton(
            btn_frame,
            text=tier_config.option1.label,
            fg_color="#2E7D32",
            hover_color="#1B5E20",
            font=ctk.CTkFont(size=12, weight="bold"),
            height=32,
            command=lambda: self._select_action(tier_config.option1),
        )
        self.btn_opt1.pack(fill="x", pady=3)

        # Option 2: Configured by Tier
        self.btn_opt2 = ctk.CTkButton(
            btn_frame,
            text=tier_config.option2.label,
            fg_color="#0277BD",
            hover_color="#01579B",
            font=ctk.CTkFont(size=12, weight="bold"),
            height=32,
            command=lambda: self._select_action(tier_config.option2),
        )
        self.btn_opt2.pack(fill="x", pady=3)

        # Option 3: Stop Search
        self.btn_stop = ctk.CTkButton(
            btn_frame,
            text="Stop Search",
            fg_color="#C62828",
            hover_color="#B71C1C",
            font=ctk.CTkFont(size=12, weight="bold"),
            height=32,
            command=lambda: self._select_action("stop"),
        )
        self.btn_stop.pack(fill="x", pady=3)

    def _select_action(self, action: Any):
        """Releases grab, destroys modal, and invokes the synchronization callback."""
        if not self._action_taken:
            self._action_taken = True
            try:
                self.grab_release()
            except Exception:
                pass
            self.destroy()
            self.callback(action)

    def _on_close(self):
        """Treat closing modal with 'X' as stopping search."""
        self._select_action("stop")


class IndexManagerDialog(ctk.CTkToplevel):
    """
    Modal dialog for managing, inspecting, and building .piidx 10-digit binary index files.
    """

    def __init__(self, parent: "PiPhoneApp"):
        super().__init__(parent)
        self.parent = parent
        self.title("10-Digit Binary Index Manager (.piidx)")
        self.geometry("460x540")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self._build_thread: Optional[threading.Thread] = None
        self._cancel_build_event = threading.Event()

        # Center over parent
        self.update_idletasks()
        try:
            x = parent.winfo_x() + (parent.winfo_width() // 2) - 230
            y = parent.winfo_y() + (parent.winfo_height() // 2) - 270
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass

        self._build_ui()
        self._refresh_status()

    def _build_ui(self):
        # Section 1: Active Index Status Card
        card_active = ctk.CTkFrame(self, fg_color="#1E1E1E", corner_radius=8)
        card_active.pack(fill="x", padx=16, pady=(14, 8))

        lbl_hdr1 = ctk.CTkLabel(
            card_active,
            text="Active 10-Digit Binary Index",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#00E5FF",
        )
        lbl_hdr1.pack(anchor="w", padx=12, pady=(10, 4))

        self.lbl_idx_file = ctk.CTkLabel(card_active, text="File: None", font=ctk.CTkFont(size=12), text_color="gray80", anchor="w")
        self.lbl_idx_file.pack(fill="x", padx=12, pady=1)

        self.lbl_idx_mode = ctk.CTkLabel(card_active, text="Mode: None", font=ctk.CTkFont(size=12), text_color="gray80", anchor="w")
        self.lbl_idx_mode.pack(fill="x", padx=12, pady=1)

        self.lbl_idx_range = ctk.CTkLabel(card_active, text="Scanned: 0 digits | Cutoff: 0", font=ctk.CTkFont(size=12), text_color="gray80", anchor="w")
        self.lbl_idx_range.pack(fill="x", padx=12, pady=1)

        self.lbl_idx_records = ctk.CTkLabel(card_active, text="Records: 0", font=ctk.CTkFont(size=12), text_color="gray80", anchor="w")
        self.lbl_idx_records.pack(fill="x", padx=12, pady=1)

        btn_row = ctk.CTkFrame(card_active, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(8, 10))

        self.btn_select_file = ctk.CTkButton(
            btn_row,
            text="📂 Load .piidx File...",
            width=150,
            height=28,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#0277BD",
            hover_color="#01579B",
            command=self._on_select_file,
        )
        self.btn_select_file.pack(side="left", padx=(0, 6))

        self.btn_unload = ctk.CTkButton(
            btn_row,
            text="❌ Unload Index",
            width=110,
            height=28,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#C62828",
            hover_color="#B71C1C",
            command=self._on_unload,
        )
        self.btn_unload.pack(side="left")

        # Section 2: Index Builder Card
        card_build = ctk.CTkFrame(self, fg_color="#1E1E1E", corner_radius=8)
        card_build.pack(fill="both", expand=True, padx=16, pady=(4, 14))

        lbl_hdr2 = ctk.CTkLabel(
            card_build,
            text="Build New Binary Index",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#4CAF50",
        )
        lbl_hdr2.pack(anchor="w", padx=12, pady=(10, 4))

        # Stream Source Selection
        lbl_src = ctk.CTkLabel(card_build, text="Pi Stream Source:", font=ctk.CTkFont(size=11), text_color="gray70", anchor="w")
        lbl_src.pack(fill="x", padx=12, pady=(2, 0))

        cached_digits_count = self.parent.engine.get_total_cached_digits()
        self.source_var = ctk.StringVar(value="local")
        self.seg_source = ctk.CTkSegmentedButton(
            card_build,
            values=["Local Pi Cache", "External File"],
            variable=self.source_var,
            font=ctk.CTkFont(size=11, weight="bold"),
            height=28,
        )
        self.seg_source.pack(fill="x", padx=12, pady=(2, 6))

        # Mode Selection
        lbl_mode = ctk.CTkLabel(card_build, text="Index Format Architecture:", font=ctk.CTkFont(size=11), text_color="gray70", anchor="w")
        lbl_mode.pack(fill="x", padx=12, pady=(2, 0))

        self.mode_var = ctk.StringVar(value="straggler")
        self.seg_mode = ctk.CTkSegmentedButton(
            card_build,
            values=["Straggler (10B Sorted)", "Full Direct (5B Array)"],
            variable=self.mode_var,
            font=ctk.CTkFont(size=11, weight="bold"),
            height=28,
        )
        self.seg_mode.pack(fill="x", padx=12, pady=(2, 6))

        # Cutoff Input
        cutoff_row = ctk.CTkFrame(card_build, fg_color="transparent")
        cutoff_row.pack(fill="x", padx=12, pady=(2, 6))
        lbl_cutoff = ctk.CTkLabel(cutoff_row, text="Straggler Cutoff Pos:", font=ctk.CTkFont(size=11), text_color="gray70")
        lbl_cutoff.pack(side="left", padx=(0, 8))
        self.entry_cutoff = ctk.CTkEntry(cutoff_row, width=120, height=26, font=ctk.CTkFont(size=12))
        self.entry_cutoff.insert(0, "0")
        self.entry_cutoff.pack(side="left")

        # Progress bar
        self.build_prog = ctk.CTkProgressBar(card_build, mode="indeterminate", height=6, progress_color="#4CAF50")
        self.build_prog.pack(fill="x", padx=12, pady=(6, 2))
        self.build_prog.set(0)

        # Telemetry
        self.lbl_build_status = ctk.CTkLabel(
            card_build,
            text=f"Ready to build index ({cached_digits_count:,} cached digits available).",
            font=ctk.CTkFont(size=11),
            text_color="gray70",
            anchor="w",
        )
        self.lbl_build_status.pack(fill="x", padx=12, pady=(1, 8))

        # Start / Cancel Build Buttons
        build_action_row = ctk.CTkFrame(card_build, fg_color="transparent")
        build_action_row.pack(fill="x", padx=12, pady=(0, 10))

        self.btn_start_build = ctk.CTkButton(
            build_action_row,
            text="⚡ Start Build",
            width=140,
            height=32,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#2E7D32",
            hover_color="#1B5E20",
            command=self._on_start_build,
        )
        self.btn_start_build.pack(side="left", padx=(0, 8))

        self.btn_cancel_build = ctk.CTkButton(
            build_action_row,
            text="Cancel",
            width=80,
            height=32,
            font=ctk.CTkFont(size=12),
            fg_color="#37474F",
            hover_color="#455A64",
            state="disabled",
            command=self._on_cancel_build,
        )
        self.btn_cancel_build.pack(side="left")

    def _refresh_status(self):
        b_idx = self.parent.engine.binary_index
        if b_idx is not None:
            self.lbl_idx_file.configure(text=f"File: {b_idx.path.name} ({b_idx.file_size / (1024*1024):.1f} MB)", text_color="#E0E0E0")
            mode_name = "Mode 1: STRAGGLER_SORTED_10B" if b_idx.mode == BinaryIndexMode.STRAGGLER_SORTED_10B else "Mode 2: FULL_DIRECT_5B"
            self.lbl_idx_mode.configure(text=f"Mode: {mode_name}", text_color="#E0E0E0")
            self.lbl_idx_range.configure(
                text=f"Scanned: {b_idx.max_pi_digit_scanned:,} digits | Cutoff: {b_idx.cutoff_pos:,}",
                text_color="#E0E0E0",
            )
            self.lbl_idx_records.configure(text=f"Records: {b_idx.record_count:,} pairs", text_color="#E0E0E0")
            self.btn_unload.configure(state="normal")
        else:
            self.lbl_idx_file.configure(text="File: None (Standard Live Engine fallback active)", text_color="gray60")
            self.lbl_idx_mode.configure(text="Mode: None", text_color="gray60")
            self.lbl_idx_range.configure(text="Scanned: 0 digits | Cutoff: 0", text_color="gray60")
            self.lbl_idx_records.configure(text="Records: 0", text_color="gray60")
            self.btn_unload.configure(state="disabled")

        self.parent._refresh_engine_ui()

    def _on_select_file(self):
        from tkinter import filedialog, messagebox
        f = filedialog.askopenfilename(
            parent=self,
            title="Select 10-Digit Binary Index (.piidx)",
            filetypes=[("Pi Binary Index (*.piidx)", "*.piidx"), ("All Files (*.*)", "*.*")],
        )
        if f:
            def worker():
                try:
                    self.parent.engine.load_binary_index(f)
                    def on_done():
                        self._refresh_status()
                        messagebox.showinfo("Index Loaded", f"Successfully loaded index:\n{f}", parent=self)
                    self.after(0, on_done)
                except Exception as e:
                    err_msg = str(e)
                    def on_err():
                        messagebox.showerror("Index Error", f"Failed to load binary index:\n{err_msg}", parent=self)
                    self.after(0, on_err)

            threading.Thread(target=worker, daemon=True).start()

    def _on_unload(self):
        def worker():
            self.parent.engine.unload_binary_index()
            gc.collect()
            self.after(0, self._refresh_status)

        threading.Thread(target=worker, daemon=True).start()

    def _on_start_build(self):
        from tkinter import filedialog, messagebox
        from pi_index_builder import BinaryIndexBuilder

        src_type = self.source_var.get()
        mode_val = (
            BinaryIndexMode.FULL_DIRECT_5B
            if "Direct" in self.seg_mode.get()
            else BinaryIndexMode.STRAGGLER_SORTED_10B
        )

        try:
            cutoff = int(self.entry_cutoff.get().strip().replace(",", ""))
        except ValueError:
            cutoff = 0

        ext_input_path = None
        if src_type != "local":
            ext_input_path = filedialog.askopenfilename(
                parent=self,
                title="Select Input Pi Stream (.txt, .zst, .gz, y-cruncher)",
                filetypes=[
                    ("Pi Stream Files (*.txt;*.zst;*.gz)", "*.txt;*.zst;*.gz"),
                    ("Text Files (*.txt)", "*.txt"),
                    ("All Files (*.*)", "*.*"),
                ],
            )
            if not ext_input_path:
                return

        out_path = self.parent.engine.data_dir / "pi_10digit.piidx"

        self._cancel_build_event.clear()
        self.btn_start_build.configure(state="disabled")
        self.btn_cancel_build.configure(state="normal")
        self.build_prog.start()
        self.lbl_build_status.configure(text="Initializing index build...", text_color="#FFA000")

        def worker():
            try:
                builder = BinaryIndexBuilder(
                    mode=mode_val,
                    cutoff_pos=cutoff,
                    output_path=out_path,
                )

                def prog(scanned: int, unique: int, rate: float):
                    txt = f"Scanned: {scanned:,} digits | Unique: {unique:,} | {rate/1_000_000:.2f}M digits/s"
                    self.after(0, lambda: self.lbl_build_status.configure(text=txt))

                if src_type == "local":
                    digits_str = self.parent.engine.digits
                    if not digits_str:
                        raise ValueError("No local Pi digits cached to build index from.")
                    final_path = builder.build_from_string(
                        digits_str,
                        progress_callback=prog,
                        cancel_event=self._cancel_build_event,
                    )
                else:
                    final_path = builder.build_from_file(
                        ext_input_path,
                        progress_callback=prog,
                        cancel_event=self._cancel_build_event,
                    )

                if self._cancel_build_event.is_set():
                    def on_cancelled():
                        self.build_prog.stop()
                        self.lbl_build_status.configure(text="Build cancelled.", text_color="#EF5350")
                        self.btn_start_build.configure(state="normal")
                        self.btn_cancel_build.configure(state="disabled")
                    self.after(0, on_cancelled)
                    return

                def on_done():
                    self.build_prog.stop()
                    self.parent.engine.load_binary_index(final_path)
                    self._refresh_status()
                    self.lbl_build_status.configure(
                        text=f"Build Complete! Indexed {builder.unique_count:,} numbers ({builder.current_pos:,} digits).",
                        text_color="#4CAF50",
                    )
                    self.btn_start_build.configure(state="normal")
                    self.btn_cancel_build.configure(state="disabled")
                    messagebox.showinfo(
                        "Build Complete",
                        f"10-Digit Binary Index built and loaded successfully!\n\n"
                        f"Scanned: {builder.current_pos:,} digits\n"
                        f"Indexed: {builder.unique_count:,} unique numbers\n"
                        f"File: {final_path.name}",
                        parent=self,
                    )

                self.after(0, on_done)
            except Exception as e:
                err_msg = str(e)
                def on_err():
                    self.build_prog.stop()
                    self.lbl_build_status.configure(text=f"Build failed: {err_msg}", text_color="#EF5350")
                    self.btn_start_build.configure(state="normal")
                    self.btn_cancel_build.configure(state="disabled")
                    messagebox.showerror("Build Error", f"Failed to build binary index:\n{err_msg}", parent=self)

                self.after(0, on_err)
            finally:
                if "builder" in locals():
                    del builder
                gc.collect()

        self._build_thread = threading.Thread(target=worker, daemon=True)
        self._build_thread.start()

    def _on_cancel_build(self):
        self._cancel_build_event.set()
        self.btn_cancel_build.configure(state="disabled", text="Stopping...")
        self.lbl_build_status.configure(text="Cancelling build...", text_color="#FF9800")


class AriaAnnouncementLabel(ctk.CTkLabel):
    """
    Dedicated accessible live status label for Windows Narrator and NVDA screen readers.
    Updates dynamically when search results load, seed expansion completes, or index status changes.
    """

    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            text="",
            font=ctk.CTkFont(size=10),
            text_color="gray45",
            anchor="w",
            **kwargs,
        )

    def announce(self, message: str):
        """Updates text and generates live region change notification."""
        self.configure(text=f"📢 {message}")
        try:
            self.event_generate("<<LiveRegionChanged>>")
        except Exception:
            pass


class AboutDialog(ctk.CTkToplevel):
    """
    Dedicated "About & Architecture" dialog displaying:
    - Arbitrary-precision math (mpmath/gmpy2) & Chudnovsky series.
    - Coupon Collector's probability theorem & Pi digit normality.
    - Dual-format .piidx memory-mapped architecture.
    - 1.25 GB Bitset tracking and 9-digit boundary stitching.
    - Hardware notes (RAM footprint, NVMe SSD speed, Python runtime).
    """

    def __init__(self, parent: ctk.CTk):
        super().__init__(parent)
        self.title("About PiPhone & Architecture")
        self.geometry("480x580")
        self.minsize(440, 500)
        self.transient(parent)

        # Center over parent
        try:
            x = parent.winfo_x() + (parent.winfo_width() // 2) - 240
            y = parent.winfo_y() + (parent.winfo_height() // 2) - 290
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass

        self._build_ui()

    def _build_ui(self):
        # Header banner
        header = ctk.CTkFrame(self, fg_color="#1E1E1E", corner_radius=8)
        header.pack(fill="x", padx=14, pady=(12, 6))

        lbl_title = ctk.CTkLabel(
            header,
            text="PIPHONE v2.4",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#00E5FF",
        )
        lbl_title.pack(anchor="w", padx=12, pady=(8, 2))

        lbl_sub = ctk.CTkLabel(
            header,
            text="Arbitrary-Precision Decimal Search Engine & Binary Index Architecture",
            font=ctk.CTkFont(size=11),
            text_color="gray70",
        )
        lbl_sub.pack(anchor="w", padx=12, pady=(0, 8))

        # Scrollable content frame
        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=14, pady=4)

        sections = [
            (
                "🧮 1. Mathematical Algorithms & Precision",
                "• Ramanujan-Chudnovsky Hypergeometric Series:\n"
                "  Calculates Pi to arbitrary precision with ~14.18 digits of precision per term.\n"
                "• Hardware Acceleration (gmpy2 + mpmath):\n"
                "  Uses compiled C GMP/MPFR arithmetic delivering 10x-15x faster throughput than pure Python.\n"
                "• Non-blocking IPC & Cancellable Math:\n"
                "  Deep precision runs in isolated child processes with instant OS termination (<0.05s) on user cancel."
            ),
            (
                "🎲 2. Coupon Collector's Probability Model",
                "• Is Pi normal? If every 10-digit telephone number appears with equal probability (10^-10), "
                "the expected digits required to observe ALL 10 billion phone numbers follows the Coupon Collector's Problem:\n"
                "    E[T] = 10^10 * (ln(10^10) + γ) ≈ 236.4 Billion Digits\n"
                "• Coverage Milestones:\n"
                "    - 100M Digits:  ~1.00% of all phone numbers (~100M unique matches)\n"
                "    - 500M Digits:  ~4.88% of all phone numbers\n"
                "    - 1 Billion:    ~9.52% of all phone numbers"
            ),
            (
                "⚡ 3. 10-Digit Binary Index Architecture (.piidx)",
                "• Mode 1: STRAGGLER_SORTED_10B\n"
                "  Stores 10-byte records [phone: uint40 LE][pos: uint40 LE] sorted by phone number. "
                "Searched via memory-mapped file (mmap) binary search in 10-50 microseconds with zero RAM usage.\n"
                "• Mode 2: FULL_DIRECT_5B\n"
                "  O(1) direct lookup table indexed by phone integer offset (16 + phone * 5 bytes).\n"
                "• Rolling Window & 9-Digit Boundary Stitch:\n"
                "  window = ((window % 1,000,000,000) * 10) + digit streams across 32MB chunks without missing straddling digits.\n"
                "• 1.25 GB Bitset Tracking (10^10 bits):\n"
                "  Guarantees first observed starting position in O(1) bit operations with crash checkpoints."
            ),
            (
                "💻 4. Hardware & Memory Resource Profile",
                "• Memory Footprint:\n"
                "  - Default App: ~60-120 MB RAM (mmap pages demand-paged by OS kernel).\n"
                "  - 100M Seed String: ~100 MB ASCII in memory.\n"
                "  - Index Builder: 1.25 GB allocated temporarily for bitset, automatically freed with gc.collect().\n"
                "• Storage Recommendations:\n"
                "  Fast NVMe SSD recommended for random O(log N) binary search disk reads.\n"
                "• Accessibility:\n"
                "  Keyboard navigation (Tab / Return / Ctrl+F) and screen reader live region announcements."
            ),
        ]

        for sec_title, sec_text in sections:
            card = ctk.CTkFrame(scroll, fg_color="#1E1E1E", corner_radius=6)
            card.pack(fill="x", pady=4)
            lbl_st = ctk.CTkLabel(
                card,
                text=sec_title,
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color="#00E5FF",
                anchor="w",
            )
            lbl_st.pack(fill="x", padx=10, pady=(8, 2))
            lbl_desc = ctk.CTkLabel(
                card,
                text=sec_text,
                font=ctk.CTkFont(size=11),
                text_color="gray85",
                justify="left",
                anchor="w",
                wraplength=420,
            )
            lbl_desc.pack(fill="x", padx=10, pady=(2, 8))

        # Close button
        btn_close = ctk.CTkButton(
            self,
            text="Close",
            width=100,
            height=30,
            command=self.destroy,
            fg_color="#37474F",
            hover_color="#455A64",
        )
        btn_close.pack(pady=(6, 12))


class ResultDisplayManager:
    """
    Manages the interactive, mouse-selectable Pi digit result display in a borderless tk.Text widget.
    Renders preceding and following context in 14pt muted text, and the matched target sequence in
    24pt bold bright accent text. Supports full mouse drag selection and Ctrl+C clipboard copying
    while strictly preventing keyboard modification.
    """

    def __init__(self, txt_widget: tk.Text):
        self.txt = txt_widget
        self.preceding = ""
        self.target = "—"
        self.following = ""
        self._setup_bindings()
        self.render()

    def _setup_bindings(self):
        def on_key(event):
            # Allow Ctrl+C and Ctrl+A for clipboard copying
            if (event.state & 4) and event.keysym.lower() in ("c", "a"):
                return None
            # Allow keyboard navigation keys
            if event.keysym in ("Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next"):
                return None
            # Block all typing and editing keys
            return "break"

        self.txt.bind("<Key>", on_key)
        self.txt.bind("<Control-c>", lambda e: (self.txt.event_generate("<<Copy>>"), "break")[1])
        self.txt.bind("<Control-C>", lambda e: (self.txt.event_generate("<<Copy>>"), "break")[1])
        self.txt.bind("<Control-a>", lambda e: (self.txt.tag_add("sel", "1.0", "end-1c"), "break")[1])
        self.txt.bind("<Control-A>", lambda e: (self.txt.tag_add("sel", "1.0", "end-1c"), "break")[1])

    def set_part(self, part: str, value: str):
        if part == "preceding":
            self.preceding = value
        elif part == "target":
            self.target = value
        elif part == "following":
            self.following = value
        self.render()

    def render(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")

        tgt = self.target if self.target else "—"
        tgt_len = len(tgt)

        # Dynamic target font scaling based on digit length to prevent clipping in 390px window
        if tgt_len <= 6:
            tgt_size = 26
            ctx_size = 13
            v_offset = 4
        elif tgt_len <= 8:
            tgt_size = 22
            ctx_size = 12
            v_offset = 3
        else:
            tgt_size = 19
            ctx_size = 11
            v_offset = 3

        self.txt.tag_configure("center", justify="center")
        self.txt.tag_configure("context", font=("Segoe UI", ctx_size), foreground="#9E9E9E", offset=v_offset)
        self.txt.tag_configure("target", font=("Segoe UI", tgt_size, "bold"), foreground="#4CAF50")

        # Assemble formatted result with context styling
        if self.preceding:
            self.txt.insert("end", self.preceding + " ", ("center", "context"))
        self.txt.insert("end", tgt, ("center", "target"))
        if self.following:
            self.txt.insert("end", " " + self.following, ("center", "context"))


class _ResultPartProxy:
    """Proxy providing configure(text=...) interface for preceding, target, and following parts."""

    def __init__(self, manager: ResultDisplayManager, part: str):
        self.manager = manager
        self.part = part

    def configure(self, **kwargs):
        if "text" in kwargs:
            self.manager.set_part(self.part, kwargs["text"])

    def cget(self, key: str):
        if key == "text":
            return getattr(self.manager, self.part)
        return ""


class ReadonlySelectableEntry(tk.Entry):
    """
    Borderless, read-only Entry widget that permits mouse drag text selection
    and Ctrl+C copying while strictly preventing keyboard modification.
    Supports configure(text=...) for seamless compatibility with CTkLabel call signatures.
    """

    def __init__(
        self,
        master,
        text: str = "",
        font=("Segoe UI", 12),
        fg: str = "gray75",
        bg: str = "#1E1E1E",
        justify: str = "center",
        **kwargs,
    ):
        super().__init__(
            master,
            relief="flat",
            highlightthickness=0,
            bg=bg,
            readonlybackground=bg,
            fg=fg,
            font=font,
            justify=justify,
            insertwidth=0,
            exportselection=True,
            **kwargs,
        )
        self.bind("<Control-c>", lambda e: (self.event_generate("<<Copy>>"), "break")[1])
        self.bind("<Control-C>", lambda e: (self.event_generate("<<Copy>>"), "break")[1])
        self.bind("<Control-a>", lambda e: (self.select_range(0, "end"), "break")[1])
        self.bind("<Control-A>", lambda e: (self.select_range(0, "end"), "break")[1])
        self.set_text(text)

    def set_text(self, text: str):
        self.configure(state="normal")
        self.delete(0, "end")
        self.insert(0, text)
        self.xview_moveto(0)
        self.configure(state="readonly")

    def configure(self, **kwargs):
        if "text" in kwargs:
            self.set_text(kwargs.pop("text"))
        if "text_color" in kwargs:
            kwargs["fg"] = kwargs.pop("text_color")
        if kwargs:
            super().configure(**kwargs)

    def cget(self, key: str):
        if key == "text":
            return self.get()
        return super().cget(key)


class PiPhoneApp(ctk.CTk):
    """
    Main PiPhone application window.
    Features a 2-tab interface:
      1. Pi Dialer: Keypad, live search telemetry, and visual match context.
      2. Search History: Persisted log of historical searches and total digits banner.
    """

    def __init__(self):
        super().__init__()

        # Window configuration (sleek smartphone aspect ratio)
        self.title("PiPhone")
        self.geometry("390x720")
        self.minsize(380, 680)

        # Core engine & concurrency
        self.engine = PiEngine(data_dir="data", default_seed_digits=100_000_000, load_seed_pack=True)
        self._search_thread: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self._msg_queue: queue.Queue = queue.Queue()
        self._is_searching = False

        # Engine Mode & Benchmark Comparison State
        self._engine_mode: SearchEngineMode = SearchEngineMode.STANDARD_ENGINE
        self._compare_mode: bool = False

        # Main layout container
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Header status bar
        self._build_header()

        # Tabview navigation container (Dialer & Search History)
        self.tabs = ctk.CTkTabview(self, corner_radius=8, command=self._on_tab_changed)
        self.tabs.grid(row=1, column=0, padx=16, pady=(0, 16), sticky="nsew")

        self.tab_dialer = self.tabs.add("Pi Dialer")
        self.tab_history = self.tabs.add("Search History")

        self._build_dialer_tab()
        self._build_history_tab()

        # Bind keyboard & numpad input shortcuts
        self.bind("<Key>", self._on_key_press)
        self.bind("<Control-f>", self._on_focus_search)
        self.bind("<Control-F>", self._on_focus_search)

        # Set initial focus to input field for immediate keyboard entry
        self.after(100, lambda: self.number_entry.focus_set())

        # Start background UI polling
        self._poll_queue()

    # =========================================================================
    # Header Bar
    # =========================================================================
    def _build_header(self):
        """Top bar displaying app branding, About button, and live engine state."""
        self.header_frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.header_frame.grid(row=0, column=0, padx=16, pady=(12, 8), sticky="ew")
        self.header_frame.grid_columnconfigure(0, weight=1)

        self.lbl_title = ctk.CTkLabel(
            self.header_frame,
            text="PIPHONE",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        self.lbl_title.grid(row=0, column=0, sticky="w")

        self.btn_about = ctk.CTkButton(
            self.header_frame,
            text="ⓘ About",
            width=65,
            height=24,
            font=ctk.CTkFont(size=11),
            fg_color="#2B2B2B",
            hover_color="#3A3A3A",
            command=self._open_about_dialog,
        )
        self.btn_about.grid(row=0, column=1, padx=(6, 8), sticky="e")

        self.lbl_status = ctk.CTkLabel(
            self.header_frame,
            text="● Ready",
            text_color="#4CAF50",
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.lbl_status.grid(row=0, column=2, sticky="e")

    def _open_about_dialog(self):
        """Opens the About & Architecture Dialog."""
        AboutDialog(self)

    # =========================================================================
    # Tab 1: Pi Dialer
    # =========================================================================
    def _build_dialer_tab(self):
        """Builds the telephone dial pad, input field, action controls, and visual display."""
        self.tab_dialer.grid_columnconfigure((0, 1, 2), weight=1)
        self.tab_dialer.grid_rowconfigure((1, 2, 3, 4), weight=1)

        # Input display with 1-10 digit strict validation
        vcmd = (self.register(self._validate_input_change), "%P")
        self.number_entry = ctk.CTkEntry(
            self.tab_dialer,
            placeholder_text="Enter 1-10 digits...",
            justify="center",
            font=ctk.CTkFont(size=22, weight="bold"),
            height=44,
            validate="key",
            validatecommand=vcmd,
        )
        self.number_entry.grid(row=0, column=0, columnspan=3, padx=16, pady=(6, 4), sticky="ew")
        self.number_entry.bind("<KeyRelease>", lambda e: self._update_action_button_state())

        # Search Engine Selector & Benchmark Compare Bar
        self.engine_frame = ctk.CTkFrame(self.tab_dialer, fg_color="#1E1E1E", corner_radius=8)
        self.engine_frame.grid(row=1, column=0, columnspan=3, padx=16, pady=(0, 4), sticky="ew")
        self.engine_frame.grid_columnconfigure(0, weight=1)

        # Segmented engine selector (Standard vs Binary Index)
        self.seg_engine = ctk.CTkSegmentedButton(
            self.engine_frame,
            values=["Standard Live", "Binary Index (.piidx)"],
            command=self._on_engine_mode_changed,
            font=ctk.CTkFont(size=11, weight="bold"),
            height=26,
        )
        self.seg_engine.set("Standard Live")
        self.seg_engine.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 2))

        # Sub-controls: Compare Both Checkbox, Index Status Pill, and Index Manager Button
        ctrl_sub = ctk.CTkFrame(self.engine_frame, fg_color="transparent")
        ctrl_sub.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 4))
        ctrl_sub.grid_columnconfigure(1, weight=1)

        self.chk_compare = ctk.CTkCheckBox(
            ctrl_sub,
            text="Compare Both",
            font=ctk.CTkFont(size=11),
            height=18,
            command=self._on_compare_toggled,
        )
        self.chk_compare.grid(row=0, column=0, sticky="w")

        self.lbl_index_status = ctk.CTkLabel(
            ctrl_sub,
            text=self._get_index_status_str(),
            font=ctk.CTkFont(size=10),
            text_color="#00E5FF",
        )
        self.lbl_index_status.grid(row=0, column=1, sticky="ew", padx=4)

        self.btn_index_mgr = ctk.CTkButton(
            ctrl_sub,
            text="⚙️ Index",
            width=62,
            height=20,
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color="#37474F",
            hover_color="#455A64",
            command=self._open_index_manager,
        )
        self.btn_index_mgr.grid(row=0, column=2, sticky="e")

        # Constrained dial pad layout for crisp smartphone proportions (padx=28)
        self.keypad_frame = ctk.CTkFrame(self.tab_dialer, fg_color="transparent")
        self.keypad_frame.grid(row=2, column=0, columnspan=3, padx=28, pady=(1, 4), sticky="nsew")
        self.keypad_frame.grid_columnconfigure((0, 1, 2), weight=1)
        self.keypad_frame.grid_rowconfigure((0, 1, 2, 3), weight=1)

        keypad_buttons = [
            ("1", 0, 0), ("2", 0, 1), ("3", 0, 2),
            ("4", 1, 0), ("5", 1, 1), ("6", 1, 2),
            ("7", 2, 0), ("8", 2, 1), ("9", 2, 2),
            ("CLR", 3, 0), ("0", 3, 1), ("⌫", 3, 2),
        ]

        for text, r, c in keypad_buttons:
            if text == "CLR":
                cmd = self._clear_input
                fg = "#37474F"
                hover = "#455A64"
            elif text == "⌫":
                cmd = self._backspace_digit
                fg = "#37474F"
                hover = "#455A64"
            else:
                cmd = lambda val=text: self._append_digit(val)
                fg = "#2B2B2B"
                hover = "#3A3A3A"

            btn = ctk.CTkButton(
                self.keypad_frame,
                text=text,
                font=ctk.CTkFont(size=22, weight="bold"),
                corner_radius=22,
                height=46,
                fg_color=fg,
                hover_color=hover,
                command=cmd,
            )
            btn.grid(row=r, column=c, padx=5, pady=3, sticky="nsew")

        # Primary Action Button ("Find in Pi" <-> "Cancel")
        self.btn_action = ctk.CTkButton(
            self.tab_dialer,
            text="Find in Pi",
            fg_color="#2E7D32",
            hover_color="#1B5E20",
            font=ctk.CTkFont(size=16, weight="bold"),
            height=42,
            command=self._on_primary_action,
            state="disabled",
        )
        self.btn_action.grid(row=3, column=0, columnspan=3, padx=16, pady=(3, 5), sticky="ew")

        # Results & Flanking Context Display Container
        self.result_frame = ctk.CTkFrame(self.tab_dialer, fg_color="#1E1E1E", corner_radius=8)
        self.result_frame.grid(row=4, column=0, columnspan=3, padx=16, pady=(1, 6), sticky="ew")
        self.result_frame.grid_columnconfigure(0, weight=1)

        # Fixed-height container frame for the mixed-font result line (prevents vertical clipping)
        self.result_txt_container = ctk.CTkFrame(
            self.result_frame,
            fg_color="#1E1E1E",
            height=50,
            corner_radius=0,
        )
        self.result_txt_container.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 2))
        self.result_txt_container.grid_propagate(False)
        self.result_txt_container.grid_columnconfigure(0, weight=1)
        self.result_txt_container.grid_rowconfigure(0, weight=1)

        # Interactive, mouse-selectable Pi sequence display (borderless tk.Text with tags)
        self.txt_result = tk.Text(
            self.result_txt_container,
            height=1,
            font=("Segoe UI", 26, "bold"),
            bg="#1E1E1E",
            relief="flat",
            highlightthickness=0,
            cursor="xterm",
            exportselection=True,
            wrap="none",
            insertwidth=0,
        )
        self.txt_result.grid(row=0, column=0, sticky="nsew", ipady=3)

        self.result_manager = ResultDisplayManager(self.txt_result)
        self.lbl_preceding = _ResultPartProxy(self.result_manager, "preceding")
        self.lbl_target = _ResultPartProxy(self.result_manager, "target")
        self.lbl_following = _ResultPartProxy(self.result_manager, "following")

        # Telemetry & exact position info (borderless readonly selectable entries)
        self.lbl_position = ReadonlySelectableEntry(
            self.result_frame,
            text="Enter a number and tap 'Find in Pi'",
            font=("Segoe UI", 12, "bold"),
            fg="gray75",
            bg="#1E1E1E",
        )
        self.lbl_position.grid(row=1, column=0, sticky="ew", padx=6, pady=(1, 1))

        # Animated indeterminate progress bar pulsing directly above the live elapsed timer
        self.prog_bar = ctk.CTkProgressBar(
            self.result_frame,
            mode="indeterminate",
            height=5,
            progress_color="#4CAF50",
            fg_color="#2E2E2E",
        )
        self.prog_bar.grid(row=2, column=0, sticky="ew", padx=16, pady=(2, 2))
        self.prog_bar.grid_remove()

        self.lbl_telemetry = ReadonlySelectableEntry(
            self.result_frame,
            text="",
            font=("Segoe UI", 10),
            fg="gray50",
            bg="#1E1E1E",
        )
        self.lbl_telemetry.grid(row=3, column=0, sticky="ew", padx=6, pady=(0, 3))

        # Comparison Card (Side-by-side performance benchmarking)
        self.compare_card = ctk.CTkFrame(self.result_frame, fg_color="#181818", corner_radius=6)
        self.compare_card.grid(row=4, column=0, sticky="ew", padx=6, pady=(2, 5))
        self.compare_card.grid_columnconfigure(0, weight=1)

        self.lbl_comp_title = ReadonlySelectableEntry(
            self.compare_card,
            text="⚡ Side-by-Side Engine Comparison",
            font=("Segoe UI", 10, "bold"),
            fg="#00E5FF",
            bg="#181818",
            justify="center",
        )
        self.lbl_comp_title.grid(row=0, column=0, sticky="ew", padx=4, pady=(3, 1))

        self.lbl_comp_std = ReadonlySelectableEntry(
            self.compare_card,
            text="",
            font=("Segoe UI", 9),
            fg="gray80",
            bg="#181818",
            justify="left",
        )
        self.lbl_comp_std.grid(row=1, column=0, sticky="ew", padx=6, pady=1)

        self.lbl_comp_bin = ReadonlySelectableEntry(
            self.compare_card,
            text="",
            font=("Segoe UI", 9),
            fg="#4CAF50",
            bg="#181818",
            justify="left",
        )
        self.lbl_comp_bin.grid(row=2, column=0, sticky="ew", padx=6, pady=1)

        self.lbl_comp_speed = ReadonlySelectableEntry(
            self.compare_card,
            text="",
            font=("Segoe UI", 9, "bold"),
            fg="#FFD54F",
            bg="#181818",
            justify="left",
        )
        self.lbl_comp_speed.grid(row=3, column=0, sticky="ew", padx=6, pady=(1, 3))
        self.compare_card.grid_remove()

        # Safe-RAM Index Miss Guard Card (Non-blocking seed expand & live burst prompt)
        self.miss_guard_card = ctk.CTkFrame(self.result_frame, fg_color="#211915", corner_radius=6)
        self.miss_guard_card.grid(row=5, column=0, sticky="ew", padx=6, pady=(2, 5))
        self.miss_guard_card.grid_columnconfigure(0, weight=1)

        self.lbl_miss_prompt = ReadonlySelectableEntry(
            self.miss_guard_card,
            text="[Digit not found in current seed cache.]",
            font=("Segoe UI", 9, "bold"),
            fg="#FFB74D",
            bg="#211915",
            justify="center",
        )
        self.lbl_miss_prompt.grid(row=0, column=0, sticky="ew", padx=4, pady=(3, 2))

        # Dynamic Seed Expansion Controls Frame (Dropdown + Button)
        self.expand_frame = ctk.CTkFrame(self.miss_guard_card, fg_color="transparent")
        self.expand_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 2))
        self.expand_frame.grid_columnconfigure(1, weight=1)

        self.opt_seed_size = ctk.CTkOptionMenu(
            self.expand_frame,
            values=["100M Digits", "500M Digits", "1B Digits"],
            width=115,
            height=26,
            font=ctk.CTkFont(size=11),
        )
        self.opt_seed_size.set("100M Digits")
        self.opt_seed_size.grid(row=0, column=0, padx=(0, 6), sticky="w")

        self.btn_expand_seed = ctk.CTkButton(
            self.expand_frame,
            text="Expand Seed Cache",
            font=ctk.CTkFont(size=11, weight="bold"),
            height=26,
            fg_color="#0277BD",
            hover_color="#01579B",
            command=self._on_expand_seed_clicked,
        )
        self.btn_expand_seed.grid(row=0, column=1, sticky="ew")

        # Expansion Progress Bar
        self.progress_expand = ctk.CTkProgressBar(self.miss_guard_card, height=6)
        self.progress_expand.set(0.0)
        self.progress_expand.grid(row=2, column=0, sticky="ew", padx=12, pady=(2, 2))
        self.progress_expand.grid_remove()

        self.btn_live_burst = ctk.CTkButton(
            self.miss_guard_card,
            text="⚡ Run Live Calculation Burst",
            font=ctk.CTkFont(size=11, weight="bold"),
            height=26,
            fg_color="#E65100",
            hover_color="#BF360C",
            command=self._on_launch_live_burst,
        )
        self.btn_live_burst.grid(row=3, column=0, sticky="ew", padx=16, pady=(2, 4))
        self.miss_guard_card.grid_remove()

        # Accessibility Screen Reader Live Region Announcement
        self.aria_label = AriaAnnouncementLabel(self.tab_dialer)
        self.aria_label.grid(row=5, column=0, columnspan=3, padx=16, pady=(1, 2), sticky="ew")

    # =========================================================================
    # Engine Selection, Benchmark Comparison & Index Management
    # =========================================================================
    def _on_launch_live_burst(self):
        """Switches engine to Standard Live and begins search upon user confirmation."""
        self.miss_guard_card.grid_remove()
        self.seg_engine.set("Standard Live")
        self._engine_mode = SearchEngineMode.STANDARD_ENGINE
        self._start_search()

    def _on_focus_search(self, event=None):
        """Switches to the Pi Dialer tab and focuses the number entry field."""
        self.tabs.set("Pi Dialer")
        self.number_entry.focus_set()
        if hasattr(self.number_entry, "_entry"):
            self.number_entry._entry.focus_set()
            self.number_entry.select_range(0, "end")
        return "break"

    def _on_expand_seed_clicked(self):
        """
        Initiates dynamic seed expansion to 100M, 500M, or 1B digits in a background daemon thread.
        Displays confirmation prompt with memory and execution time warnings prior to allocating RAM.
        """
        from tkinter import messagebox

        if self._is_searching:
            return

        choice = self.opt_seed_size.get()
        target_len = 100_000_000
        if "500M" in choice:
            target_len = 500_000_000
        elif "1B" in choice:
            target_len = 1_000_000_000

        current_cached = self.engine.get_total_cached_digits()
        if target_len <= current_cached:
            messagebox.showinfo(
                "Seed Expansion",
                f"The seed cache already contains {current_cached:,} digits, "
                f"which meets or exceeds the selected target ({target_len:,} digits).",
                parent=self,
            )
            return

        # RAM & Time estimations
        if target_len <= 100_000_000:
            ram_est = "~150 MB"
            time_est = "~30–60 seconds"
        elif target_len <= 500_000_000:
            ram_est = "~600 MB"
            time_est = "~1–3 minutes"
        else:
            ram_est = "~1.2 GB"
            time_est = "~4–8 minutes"

        confirm_msg = (
            f"Expand Pi seed cache to {target_len:,} digits?\n\n"
            f"• Current Cache: {current_cached:,} digits\n"
            f"• Target Cache:  {target_len:,} digits (+{target_len - current_cached:,} digits)\n"
            f"• Estimated RAM: {ram_est}\n"
            f"• Estimated Time: {time_est}\n\n"
            f"Calculation will proceed asynchronously in the background."
        )

        if not messagebox.askyesno("Confirm Seed Expansion", confirm_msg, parent=self):
            return

        self.btn_expand_seed.configure(state="disabled", text="Expanding...")
        self.btn_action.configure(state="disabled")
        self.lbl_status.configure(text="● Expanding...", text_color="#FFA000")
        self.progress_expand.grid()
        self.progress_expand.set(0.0)
        self.lbl_miss_prompt.configure(text=f"[Expanding seed to {target_len:,} digits...]")
        self.aria_label.announce(f"Expanding seed cache to {target_len:,} digits. Please wait.")

        def _worker():
            cancel_ev = threading.Event()
            def progress(pct: float, msg: str):
                self._msg_queue.put(("expand_progress", (pct, msg)))
            try:
                new_len = self.engine.expand_seed(
                    target_len=target_len,
                    progress_callback=progress,
                    cancel_event=cancel_ev,
                )
                self._msg_queue.put(("expand_done", new_len))
            except Exception as exc:
                self._msg_queue.put(("expand_error", str(exc)))

        threading.Thread(target=_worker, daemon=True).start()

    def _get_index_status_str(self) -> str:
        """Returns a compact status string describing the currently loaded binary index."""
        if self.engine.binary_index is not None:
            mode_str = "Straggler" if self.engine.binary_index.mode == 1 else "Direct"
            scanned_m = self.engine.binary_index.max_pi_digit_scanned / 1_000_000
            return f"● Idx: {mode_str} ({scanned_m:.0f}M)"
        return "○ Idx: None"

    def _refresh_engine_ui(self):
        """Updates the binary index status label and colors in the Dialer tab."""
        status_txt = self._get_index_status_str()
        color = "#00E5FF" if self.engine.binary_index is not None else "gray50"
        self.lbl_index_status.configure(text=status_txt, text_color=color)

    def _on_engine_mode_changed(self, value: str):
        """Handles user changing the segmented engine switch."""
        if "Binary" in value:
            self._engine_mode = SearchEngineMode.BINARY_INDEX_ENGINE
        else:
            self._engine_mode = SearchEngineMode.STANDARD_ENGINE
        self.aria_label.announce(f"Switched engine mode to {value}.")

    def _on_compare_toggled(self):
        """Handles toggling of the 'Compare Both' checkbox."""
        self._compare_mode = bool(self.chk_compare.get())

    def _open_index_manager(self):
        """Opens the 10-Digit Binary Index Manager dialog modal."""
        dialog = IndexManagerDialog(self)
        dialog.grab_set()

    def _on_compare_completed(
        self,
        res_std: Optional[SearchResult],
        res_bin: Optional[SearchResult],
        speedup: float,
    ):
        """Displays side-by-side benchmark comparison metrics in the compare card."""
        self.compare_card.grid()

        if res_std is not None and res_std.position > 0:
            std_time = format_latency(res_std.elapsed_seconds)
            self.lbl_comp_std.configure(
                text=f"Standard: Digit #{res_std.position:,} in {std_time} ({res_std.source})"
            )
        elif res_std is not None and res_std.position == 0:
            self.lbl_comp_std.configure(text=f"Standard: {res_std.source}")
        else:
            self.lbl_comp_std.configure(text="Standard: Search stopped / not found")

        if res_bin is not None and res_bin.position > 0:
            bin_time = format_latency(res_bin.elapsed_seconds)
            self.lbl_comp_bin.configure(
                text=f"Binary Idx: Digit #{res_bin.position:,} in {bin_time} ({res_bin.source})"
            )
        elif res_bin is not None and res_bin.position == 0:
            self.lbl_comp_bin.configure(text=f"Binary Idx: {res_bin.source}")
        else:
            self.lbl_comp_bin.configure(text="Binary Idx: Search stopped / not found")

        if speedup is not None and speedup > 0:
            self.lbl_comp_speed.configure(
                text=f"⚡ Speedup: Binary Index is {speedup:,.1f}x faster!"
            )
        else:
            self.lbl_comp_speed.configure(text="⚡ Speedup: Both engines identical or N/A")

        # Update primary dialer result display with best result
        best_res = None
        if res_bin is not None and res_bin.position > 0:
            best_res = res_bin
        elif res_std is not None and res_std.position > 0:
            best_res = res_std
        else:
            best_res = res_bin or res_std

        if best_res is not None:
            self._on_search_completed(best_res, is_comparison=True)

    # =========================================================================
    # Tab 2: Search History
    # =========================================================================
    def _build_history_tab(self):
        """Builds the search history list with total cached digits banner."""
        self.tab_history.grid_columnconfigure(0, weight=1)
        self.tab_history.grid_rowconfigure(0, weight=0)
        self.tab_history.grid_rowconfigure(1, weight=1)

        # Feature 1: Prominent banner card displaying total cached digits
        self.banner_card = ctk.CTkFrame(self.tab_history, fg_color="#1E1E1E", corner_radius=8)
        self.banner_card.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 4))

        self.lbl_cached_banner = ctk.CTkLabel(
            self.banner_card,
            text=f"Total Pi Digits Cached: {self.engine.get_total_cached_digits():,}",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#00E5FF",
        )
        self.lbl_cached_banner.pack(padx=12, pady=(10, 4))

        # Seed Export & Import Action Buttons
        seed_btn_frame = ctk.CTkFrame(self.banner_card, fg_color="transparent")
        seed_btn_frame.pack(padx=12, pady=(2, 4))

        self.btn_export_seed = ctk.CTkButton(
            seed_btn_frame,
            text="📤 Export Seed",
            width=120,
            height=28,
            fg_color="#2E7D32",
            hover_color="#1B5E20",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._export_seed_dialog,
        )
        self.btn_export_seed.pack(side="left", padx=4)

        self.btn_import_seed = ctk.CTkButton(
            seed_btn_frame,
            text="📥 Import Seed",
            width=120,
            height=28,
            fg_color="#0277BD",
            hover_color="#01579B",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._import_seed_dialog,
        )
        self.btn_import_seed.pack(side="left", padx=4)

        self.lbl_seed_status = ctk.CTkLabel(
            self.banner_card,
            text="",
            font=ctk.CTkFont(size=11),
            text_color="gray70",
        )
        self.lbl_seed_status.pack(padx=12, pady=(0, 6))

        # Scrollable log of searches
        self.history_list = ctk.CTkScrollableFrame(self.tab_history)
        self.history_list.grid(row=1, column=0, sticky="nsew", padx=6, pady=(4, 6))
        self.history_list.grid_columnconfigure(0, weight=1)

        self._refresh_history_tab()

    def _on_tab_changed(self):
        """Refreshes history list and total digits banner when switching tabs."""
        if self.tabs.get() == "Search History":
            self._refresh_history_tab()

    def _refresh_history_tab(self):
        """Updates total cached digits counter and reloads history log from disk."""
        # Update total cached digits banner
        total_digits = self.engine.get_total_cached_digits()
        self.lbl_cached_banner.configure(text=f"Total Pi Digits Cached: {total_digits:,}")

        # Clear existing items
        for widget in self.history_list.winfo_children():
            widget.destroy()

        history_file = self.engine.history_file
        items = []
        if history_file.exists():
            try:
                with open(history_file, "r", encoding="utf-8") as f:
                    items = json.load(f)
            except Exception:
                items = []

        if not items:
            lbl_empty = ctk.CTkLabel(
                self.history_list,
                text="No searches recorded yet.",
                text_color="gray50",
                font=ctk.CTkFont(size=13),
            )
            lbl_empty.pack(pady=30)
            return

        # Show newest queries first
        for entry in reversed(items):
            card = ctk.CTkFrame(self.history_list, fg_color="#232323", corner_radius=6)
            card.pack(fill="x", padx=2, pady=3)
            card.grid_columnconfigure(0, weight=1)
            card.grid_columnconfigure(1, weight=0)

            query = entry.get("query", "")
            pos = entry.get("position", 0)
            pre = entry.get("preceding", "")
            tgt = entry.get("target", query)
            flw = entry.get("following", "")
            source = entry.get("source", "Calculated")
            elapsed = entry.get("elapsed_seconds", 0.0)
            ts = entry.get("timestamp", "")

            # Compact metadata formatting for 390px window
            if "Index Cache" in source or "Sorted Index" in source:
                src_label = "Cache"
            elif "Binary Index" in source:
                src_label = "Binary Idx"
            elif "Fallback" in source:
                src_label = "Idx Fallback"
            elif "Calculated" in source:
                new_d = entry.get("new_digits_calculated", 0)
                if new_d > 0:
                    src_label = f"Calc {format_delta_digits(new_d).replace(' digits', '')}"
                else:
                    src_label = "Calc"
            else:
                src_label = source

            if elapsed >= 1.0:
                time_str = f"{elapsed:.1f}s"
            else:
                time_str = format_latency(elapsed)

            if len(ts) >= 16 and ts[4] == "-" and ts[7] == "-":
                ts_compact = f"{ts[5:7]}/{ts[8:10]} {ts[11:16]}"
            else:
                ts_compact = ts

            # Row 0, Col 0: Title (Query & Position)
            title_text = f"{query}  →  Digit #{pos:,}"
            lbl_title = ReadonlySelectableEntry(
                card,
                text=title_text,
                font=("Segoe UI", 13, "bold"),
                fg="#E0E0E0",
                bg="#232323",
                justify="left",
            )
            lbl_title.grid(row=0, column=0, sticky="ew", padx=(10, 4), pady=(5, 0))

            # Row 0, Col 1: View button in top-right corner
            btn_view = ctk.CTkButton(
                card,
                text="View",
                width=48,
                height=24,
                fg_color="#37474F",
                hover_color="#455A64",
                font=ctk.CTkFont(size=11, weight="bold"),
                command=lambda q=query, p=pos, pr=pre, t=tgt, f=flw, s=source, e=elapsed: self._load_history_result(
                    q, p, pr, t, f, s, e
                ),
            )
            btn_view.grid(row=0, column=1, padx=(4, 10), pady=(5, 0), sticky="e")

            # Row 1, Col 0 (columnspan=2): Context snippet (full card width)
            if not pre and not flw:
                context_text = f"Target: [{tgt}]"
            else:
                context_text = f"Context: {pre} [{tgt}] {flw}"
            lbl_context = ReadonlySelectableEntry(
                card,
                text=context_text,
                font=("Segoe UI", 12),
                fg="#4CAF50",
                bg="#232323",
                justify="left",
            )
            lbl_context.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(1, 2))

            # Row 2, Col 0 (columnspan=2): Source • Time • Compact Timestamp
            sub_info = f"{src_label} ({time_str}) • {ts_compact}"
            lbl_sub = ReadonlySelectableEntry(
                card,
                text=sub_info,
                font=("Segoe UI", 10),
                fg="gray60",
                bg="#232323",
                justify="left",
            )
            lbl_sub.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 5))

    def _load_history_result(
        self,
        query: str,
        pos: int,
        pre: str,
        target: str,
        flw: str,
        source: str,
        elapsed: float,
    ):
        """Loads a past search result into the Pi Dialer tab."""
        self.number_entry.delete(0, "end")
        self.number_entry.insert(0, query)
        self._update_action_button_state()

        self.lbl_preceding.configure(text=pre)
        self.lbl_target.configure(text=target)
        self.lbl_following.configure(text=flw)
        self.lbl_position.configure(text=f"Found at decimal digit #{pos:,}")
        self.lbl_telemetry.configure(text=f"Source: {source} ({elapsed*1000:.1f}ms)")
        self.lbl_status.configure(text="● Match Found", text_color="#4CAF50")

        self.tabs.set("Pi Dialer")

    # =========================================================================
    # Input Validation & Keypad / Keyboard Handlers
    # =========================================================================
    def _validate_input_change(self, new_val: str) -> bool:
        """Enforces that input only contains digits and never exceeds 10 digits."""
        if new_val == "":
            return True
        if not (new_val.isascii() and new_val.isdigit()):
            return False
        if len(new_val) > 10:
            return False
        return True

    def _update_action_button_state(self):
        """Enables the 'Find in Pi' button only when input contains 1-10 digits."""
        if self._is_searching:
            return
        val = self.number_entry.get().strip()
        if 1 <= len(val) <= 10 and (val.isascii() and val.isdigit()):
            self.btn_action.configure(state="normal")
        else:
            self.btn_action.configure(state="disabled")

    def _append_digit(self, char: str):
        """Appends a digit to the input entry if under 10 digits."""
        if self._is_searching:
            return
        current = self.number_entry.get()
        if len(current) < 10 and len(char) == 1 and ("0" <= char <= "9"):
            self.number_entry.insert("end", char)
            self._update_action_button_state()

    def _backspace_digit(self):
        """Deletes the trailing digit from the input field."""
        if self._is_searching:
            return
        current = self.number_entry.get()
        if current:
            self.number_entry.delete(len(current) - 1, "end")
            self._update_action_button_state()

    def _clear_input(self):
        """Clears the entry field and resets result displays."""
        if self._is_searching:
            return
        self.number_entry.delete(0, "end")
        self._update_action_button_state()
        self.compare_card.grid_remove()
        self.miss_guard_card.grid_remove()
        self.lbl_preceding.configure(text="")
        self.lbl_target.configure(text="—")
        self.lbl_following.configure(text="")
        self.lbl_position.configure(text="Enter a number and tap 'Find in Pi'")
        self.lbl_telemetry.configure(text="")
        self.lbl_status.configure(text="● Ready", text_color="#4CAF50")

    def _on_key_press(self, event):
        """
        Global keyboard & numpad event handler for the Pi Dialer tab:
        - 0-9 (top-row & numpad): appends digit (avoids double entry when entry is focused)
        - Return / KP_Enter: triggers Find in Pi
        - BackSpace: deletes trailing digit
        - Escape / Delete: clears entire input (CLR)
        """
        # Bypass when Control modifier is active (allows Ctrl+C, Ctrl+A, etc. to pass to focused widget)
        if event.state & 4:
            return

        # Only active on the Pi Dialer tab
        if self.tabs.get() != "Pi Dialer":
            return

        is_entry_focused = False
        try:
            focused = self.focus_get()
            is_entry_focused = focused in (
                self.number_entry,
                getattr(self.number_entry, "_entry", None),
            )
        except Exception:
            pass

        keysym = event.keysym

        # Return / Enter -> Find in Pi
        if keysym in ("Return", "KP_Enter"):
            if not self._is_searching:
                self._on_primary_action()
            return "break"

        # Escape / Delete -> CLR (Clear input)
        if keysym in ("Escape", "Delete"):
            self._clear_input()
            return "break"

        # BackSpace -> ⌫
        if keysym == "BackSpace":
            if not is_entry_focused:
                self._backspace_digit()
                return "break"
            else:
                self.after(10, self._update_action_button_state)
                return

        # Numeric digits 0-9 (top row or numpad)
        digit = None
        if keysym in ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9"):
            digit = keysym
        elif keysym.startswith("KP_") and len(keysym) == 4 and ("0" <= keysym[3] <= "9"):
            digit = keysym[3]

        if digit is not None:
            if not is_entry_focused:
                self._append_digit(digit)
                return "break"
            else:
                self.after(10, self._update_action_button_state)
                return

    # =========================================================================
    # Search Execution, Concurrency & Checkpoint Pausing
    # =========================================================================
    def _on_primary_action(self):
        """Handles click on the primary action button (Find in Pi or Cancel)."""
        if self._is_searching:
            self._cancel_search()
        else:
            self._start_search()

    def _start_search(self):
        """Launches the background search worker thread."""
        query = self.number_entry.get().strip()
        if not (1 <= len(query) <= 10 and query.isascii() and query.isdigit()):
            return

        self._is_searching = True
        self._cancel_event.clear()

        # Update button into Cancel state, disabled for 300ms to prevent rapid double-clicks from firing Cancel
        self.btn_action.configure(
            text="Cancel Search",
            fg_color="#C62828",
            hover_color="#B71C1C",
            state="disabled",
        )
        self.after(300, lambda: self.btn_action.configure(state="normal") if self._is_searching else None)

        self.lbl_status.configure(text="● Searching...", text_color="#FFA000")
        self.lbl_preceding.configure(text="")
        self.lbl_target.configure(text=query)
        self.lbl_following.configure(text="")
        self.lbl_position.configure(text="Searching decimal expansion of Pi...")
        self.lbl_telemetry.configure(text="Querying index cache...")
        self.compare_card.grid_remove()
        self.miss_guard_card.grid_remove()

        # Start indeterminate progress bar
        self.prog_bar.grid()
        self.prog_bar.start()

        # Spawn worker thread with 100k default stepping
        self._search_thread = threading.Thread(
            target=self._search_worker,
            args=(query,),
            daemon=True,
        )
        self._search_thread.start()

    def _cancel_search(self):
        """Signals the background calculation thread to halt cleanly."""
        self._cancel_event.set()
        self.btn_action.configure(state="disabled", text="Cancelling...")
        self.lbl_status.configure(text="● Aborting...", text_color="#FF9800")
        self.lbl_position.configure(text="Halting search...")

    def _search_worker(self, query: str):
        """
        Background worker thread executing PiEngine search.
        Supports progress updates and synchronous checkpoint modal pausing
        with the 4-tier escalating schedule.
        """
        def progress_cb(digits_searched: int, elapsed: float, status_msg: Optional[str] = None):
            self._msg_queue.put(("progress", (digits_searched, elapsed, status_msg)))

        def checkpoint_cb(
            new_digits: int,
            total_cached: int,
            tier_config: Optional[CheckpointTierConfig] = None,
        ) -> Any:
            # Check if user already clicked Cancel
            if self._cancel_event.is_set():
                return "stop"

            if tier_config is None:
                tier_config = self.engine.get_checkpoint_tier_config(total_cached)

            # Create synchronization primitives
            response_event = threading.Event()
            response_holder: dict[str, Any] = {"action": "stop"}

            # Send checkpoint request with tier_config and query to Tkinter GUI thread
            self._msg_queue.put(
                (
                    "checkpoint",
                    (new_digits, total_cached, tier_config, response_event, response_holder, query),
                )
            )

            # Wait synchronously on worker thread until modal button clicked
            response_event.wait()
            return response_holder["action"]

        if self._compare_mode:
            res_std, res_bin, speedup = self.engine.compare_engines(
                query=query,
                cancel_event=self._cancel_event,
                progress_callback=progress_cb,
                checkpoint_callback=checkpoint_cb,
            )
            if self._cancel_event.is_set():
                self._msg_queue.put(("cancelled", None))
            else:
                best_res = res_bin or res_std
                if best_res is not None:
                    self.engine.log_history(best_res)
                self._msg_queue.put(("compare_completed", (res_std, res_bin, speedup)))
        else:
            result = self.engine.search(
                query=query,
                cancel_event=self._cancel_event,
                progress_callback=progress_cb,
                checkpoint_callback=checkpoint_cb,
                engine_mode=self._engine_mode,
            )

            if self._cancel_event.is_set() or result is None:
                self._msg_queue.put(("cancelled", None))
            else:
                self.engine.log_history(result)
                self._msg_queue.put(("found", result))

    def _poll_queue(self):
        """Polls the inter-thread message queue for UI events and modal triggers."""
        try:
            while True:
                msg_type, payload = self._msg_queue.get_nowait()
                if msg_type == "progress":
                    payload_tuple = payload
                    digits_searched = payload_tuple[0]
                    elapsed = payload_tuple[1]
                    status_msg = payload_tuple[2] if len(payload_tuple) > 2 else None

                    self.prog_bar.grid()
                    self.prog_bar.start()
                    self.lbl_position.configure(text="Calculating Pi...")
                    if status_msg:
                        self.lbl_telemetry.configure(text=status_msg)
                    else:
                        self.lbl_telemetry.configure(
                            text=f"{digits_searched:,} digits searched ({elapsed:.1f}s elapsed)"
                        )
                elif msg_type == "checkpoint":
                    self.prog_bar.stop()
                    self.prog_bar.grid_remove()
                    (
                        new_digits,
                        total_cached,
                        tier_config,
                        response_event,
                        response_holder,
                        query_str,
                    ) = payload[:6]
                    self._show_checkpoint_modal(
                        new_digits,
                        total_cached,
                        tier_config,
                        response_event,
                        response_holder,
                        query=query_str,
                    )
                elif msg_type == "found":
                    self._on_search_completed(payload)
                elif msg_type == "compare_completed":
                    res_std, res_bin, speedup = payload
                    self._on_compare_completed(res_std, res_bin, speedup)
                elif msg_type == "cancelled":
                    self._on_search_cancelled()
                elif msg_type == "expand_progress":
                    pct, msg = payload
                    self.progress_expand.grid()
                    self.progress_expand.set(pct)
                    self.lbl_miss_prompt.configure(text=f"[Expanding: {pct*100:.1f}%] {msg}")
                elif msg_type == "expand_done":
                    new_total = payload
                    self.progress_expand.grid_remove()
                    self.progress_expand.set(0.0)
                    self.btn_expand_seed.configure(state="normal", text="Expand Seed Cache")
                    self.btn_action.configure(state="normal")
                    self.lbl_status.configure(text="● Ready", text_color="#4CAF50")
                    self.lbl_miss_prompt.configure(text=f"[Seed expanded to {new_total:,} digits!]")
                    self.aria_label.announce(f"Seed expansion complete. Cache now contains {new_total:,} digits.")
                    self._refresh_history_tab()
                    from tkinter import messagebox
                    messagebox.showinfo(
                        "Seed Expanded",
                        f"Successfully expanded Pi seed to {new_total:,} digits!",
                        parent=self,
                    )
                elif msg_type == "expand_error":
                    err_msg = payload
                    self.progress_expand.grid_remove()
                    self.progress_expand.set(0.0)
                    self.btn_expand_seed.configure(state="normal", text="Expand Seed Cache")
                    self.btn_action.configure(state="normal")
                    self.lbl_status.configure(text="● Error", text_color="#F44336")
                    self.lbl_miss_prompt.configure(text=f"[Expansion Failed: {err_msg[:40]}]")
                    self.aria_label.announce(f"Seed expansion failed: {err_msg}")
                    from tkinter import messagebox
                    messagebox.showerror(
                        "Expansion Error",
                        f"Failed to expand seed cache: {err_msg}",
                        parent=self,
                    )
        except queue.Empty:
            pass

        # Schedule next poll in 50ms
        self.after(50, self._poll_queue)

    def _show_checkpoint_modal(
        self,
        new_digits: int,
        total_cached: int,
        tier_config: CheckpointTierConfig,
        response_event: threading.Event,
        response_holder: dict[str, Any],
        query: str = "",
    ):
        """Opens the checkpoint pause modal with the 5-tier escalating schedule on the GUI thread."""
        def on_action(action: Any):
            response_holder["action"] = action
            if action != "stop" and action is not None:
                # Immediately show and start progress bar and set status on main GUI thread BEFORE worker resumes
                self.prog_bar.grid()
                self.prog_bar.start()
                self.lbl_position.configure(text="Calculating Pi...")
                self.lbl_telemetry.configure(text="Resuming calculation...")
                self.lbl_status.configure(text="● Calculating...", text_color="#FFA000")
                self.update()
            response_event.set()
            if action == "stop":
                self._refresh_history_tab()

        CheckpointModal(
            self,
            new_digits,
            total_cached,
            tier_config,
            on_action,
            query=query,
        )

    def _export_seed_dialog(self):
        """Opens file save dialog and exports compressed Pi seed pack in a background thread."""
        if self._is_searching:
            return

        from tkinter import filedialog, messagebox

        dest_file = filedialog.asksaveasfilename(
            parent=self,
            title="Export Pi Seed Pack",
            initialfile="pi_seed.zst",
            defaultextension=".zst",
            filetypes=[
                ("Zstandard Archive (*.zst)", "*.zst"),
                ("Gzip Archive (*.gz)", "*.gz"),
                ("Text File (*.txt)", "*.txt"),
                ("All Files (*.*)", "*.*"),
            ],
        )
        if not dest_file:
            return

        self.btn_export_seed.configure(state="disabled")
        self.btn_import_seed.configure(state="disabled")
        self.lbl_seed_status.configure(text="Compressing and exporting seed...", text_color="#FFA000")

        def worker():
            try:
                def prog(msg: str):
                    self.after(0, lambda: self.lbl_seed_status.configure(text=msg))

                meta = self.engine.export_seed(dest_file, progress_callback=prog)
                size_mb = meta["bytes"] / (1024 * 1024)
                success_text = f"Exported {meta['digits_count']:,} digits ({size_mb:.1f} MB)"

                def on_done():
                    self.lbl_seed_status.configure(text=success_text, text_color="#4CAF50")
                    self.btn_export_seed.configure(state="normal")
                    self.btn_import_seed.configure(state="normal")
                    messagebox.showinfo(
                        "Export Complete",
                        f"Successfully exported {meta['digits_count']:,} digits to:\n{dest_file}",
                        parent=self,
                    )

                self.after(0, on_done)
            except Exception as e:
                err_text = str(e)
                def on_err():
                    self.lbl_seed_status.configure(text=f"Export failed: {err_text}", text_color="#EF5350")
                    self.btn_export_seed.configure(state="normal")
                    self.btn_import_seed.configure(state="normal")
                    messagebox.showerror("Export Error", f"Failed to export seed:\n{err_text}", parent=self)

                self.after(0, on_err)

        threading.Thread(target=worker, daemon=True).start()

    def _import_seed_dialog(self):
        """Opens file open dialog and imports a Pi seed pack in a background thread."""
        if self._is_searching:
            return

        from tkinter import filedialog, messagebox

        src_file = filedialog.askopenfilename(
            parent=self,
            title="Import Pi Seed Pack",
            filetypes=[
                ("Pi Seed Archives (*.zst;*.gz;*.xz;*.txt)", "*.zst;*.gz;*.xz;*.txt"),
                ("Zstandard Archive (*.zst)", "*.zst"),
                ("Gzip Archive (*.gz)", "*.gz"),
                ("LZMA Archive (*.xz)", "*.xz"),
                ("Text File (*.txt)", "*.txt"),
                ("All Files (*.*)", "*.*"),
            ],
        )
        if not src_file:
            return

        if not messagebox.askyesno(
            "Confirm Seed Import",
            "Importing this seed will replace your current cached digits of Pi with the imported seed.\n\nDo you want to proceed?",
            parent=self,
        ):
            return

        self.btn_export_seed.configure(state="disabled")
        self.btn_import_seed.configure(state="disabled")
        self.lbl_seed_status.configure(text="Reading and decompressing seed...", text_color="#FFA000")

        def worker():
            try:
                def prog(msg: str):
                    self.after(0, lambda: self.lbl_seed_status.configure(text=msg))

                count = self.engine.import_seed(src_file, progress_callback=prog)
                success_text = f"Imported {count:,} digits successfully!"

                def on_done():
                    self.lbl_seed_status.configure(text=success_text, text_color="#4CAF50")
                    self.btn_export_seed.configure(state="normal")
                    self.btn_import_seed.configure(state="normal")
                    self._refresh_history_tab()
                    messagebox.showinfo(
                        "Import Complete",
                        f"Successfully imported {count:,} digits from:\n{src_file}",
                        parent=self,
                    )

                self.after(0, on_done)
            except Exception as e:
                err_text = str(e)
                def on_err():
                    self.lbl_seed_status.configure(text=f"Import failed: {err_text}", text_color="#EF5350")
                    self.btn_export_seed.configure(state="normal")
                    self.btn_import_seed.configure(state="normal")
                    messagebox.showerror("Import Error", f"Failed to import seed:\n{err_text}", parent=self)

                self.after(0, on_err)

        threading.Thread(target=worker, daemon=True).start()

    def _on_search_completed(self, result: SearchResult, is_comparison: bool = False):
        """Updates the dialer UI and history tab when a match or miss is processed."""
        self._is_searching = False
        self.prog_bar.stop()
        self.prog_bar.grid_remove()
        self.btn_action.configure(
            text="Find in Pi",
            fg_color="#2E7D32",
            hover_color="#1B5E20",
        )
        self._update_action_button_state()

        if result.position == 0:
            self.lbl_status.configure(text="● Not Found", text_color="#FF9800")
            self.lbl_preceding.configure(text="")
            self.lbl_target.configure(text=result.target)
            self.lbl_following.configure(text="")
            self.lbl_position.configure(text=f"Sequence {result.target} not in cached digits")
            self.lbl_telemetry.configure(text=result.source)
            self.lbl_miss_prompt.configure(
                text="[Digit not found in current seed cache.]"
            )
            self.miss_guard_card.grid()

            if not is_comparison:
                self.compare_card.grid_remove()

            self.aria_label.announce(f"Search completed: sequence {result.target} not found in current seed cache.")
            self._refresh_history_tab()
            return

        self.miss_guard_card.grid_remove()
        self.lbl_status.configure(text="● Match Found", text_color="#4CAF50")
        self.lbl_preceding.configure(text=result.preceding)
        self.lbl_target.configure(text=result.target)
        self.lbl_following.configure(text=result.following)
        self.lbl_position.configure(text=f"Found at decimal digit #{result.position:,}")

        # Distinguish cache vs newly calculated in telemetry
        if result.new_digits_calculated > 0:
            source_desc = f"Source: {result.source} ({result.elapsed_seconds:.2f}s)"
        else:
            source_desc = f"Source: {result.source} ({format_latency(result.elapsed_seconds)})"
        self.lbl_telemetry.configure(text=source_desc)

        if not is_comparison:
            self.compare_card.grid_remove()

        self.aria_label.announce(f"Match found for {result.target} at position {result.position:,}.")
        # Refresh history tab content & total digits banner
        self._refresh_history_tab()

    def _on_search_cancelled(self):
        """Resets the dialer UI and updates cached count when calculation halts."""
        self._is_searching = False
        self.prog_bar.stop()
        self.prog_bar.grid_remove()
        self.compare_card.grid_remove()
        self.miss_guard_card.grid_remove()
        self.btn_action.configure(
            text="Find in Pi",
            fg_color="#2E7D32",
            hover_color="#1B5E20",
        )
        self._update_action_button_state()

        self.lbl_status.configure(text="● Ready", text_color="#4CAF50")
        self.lbl_position.configure(text="Search stopped.")
        total_digits = self.engine.get_total_cached_digits()
        self.lbl_telemetry.configure(text=f"Total Pi Digits Cached: {total_digits:,}")
        self.aria_label.announce("Search stopped.")

        # Refresh history banner
        self._refresh_history_tab()


def main():
    app = PiPhoneApp()
    app.mainloop()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()