"""
PiPhone - Interactive Dark-Mode Pi Dialer & Decimal Expansion Search Engine
Version 1.1: Checkpoint Pausing, Total Digits Banner, Contextual Ellipses,
and Keyboard/Numpad input bindings.
"""

from __future__ import annotations

import json
import queue
import threading
import time
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

from pi_engine import (
    CheckpointTierConfig,
    PiEngine,
    SearchResult,
    format_context,
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
    ):
        super().__init__(parent)
        self.title("Search Checkpoint")
        self.geometry("420x310")
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
            y = parent.winfo_y() + (parent.winfo_height() // 2) - 155
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Telemetry & prompt description
        msg = (
            f"Match not found yet.\n\n"
            f"Calculated {new_digits:,} new digits\n"
            f"(Total Pi Digits Cached: {total_cached:,}).\n\n"
            f"Speed Tier {tier_config.tier} (Min Step est: {PiEngine.format_estimated_time(tier_config.t_min_step)})\n"
        )
        if tier_config.note:
            msg += f"\n{tier_config.note}\n\nSelect next calculation budget:"
            self.geometry("440x350")
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


class PiPhoneApp(ctk.CTk):
    """
    Main PiPhone application window.
    Features a 2-tab interface:
      1. Pi Dialer: Keypad, live search telemetry, and visual match context.
      2. Search History: Persisted log of historical searches and total digits banner.
    """

    def __init__(self):
        super().__init__()

        # Window configuration
        self.title("PiPhone")
        self.geometry("440x730")
        self.minsize(400, 680)

        # Core engine & concurrency
        self.engine = PiEngine(data_dir="data", default_seed_digits=100_000_000, load_seed_pack=True)
        self._search_thread: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self._msg_queue: queue.Queue = queue.Queue()
        self._is_searching = False

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

        # Start background UI polling
        self._poll_queue()

    # =========================================================================
    # Header Bar
    # =========================================================================
    def _build_header(self):
        """Top bar displaying app branding and live engine state."""
        self.header_frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.header_frame.grid(row=0, column=0, padx=16, pady=(12, 8), sticky="ew")
        self.header_frame.grid_columnconfigure(0, weight=1)

        self.lbl_title = ctk.CTkLabel(
            self.header_frame,
            text="PIPHONE",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        self.lbl_title.grid(row=0, column=0, sticky="w")

        self.lbl_status = ctk.CTkLabel(
            self.header_frame,
            text="● Ready",
            text_color="#4CAF50",
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.lbl_status.grid(row=0, column=1, sticky="e")

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
            font=ctk.CTkFont(size=24, weight="bold"),
            height=48,
            validate="key",
            validatecommand=vcmd,
        )
        self.number_entry.grid(row=0, column=0, columnspan=3, padx=12, pady=(10, 12), sticky="ew")
        self.number_entry.bind("<KeyRelease>", lambda e: self._update_action_button_state())

        # Dial pad layout (CLR at bottom-left, ⌫ at bottom-right)
        keypad_buttons = [
            ("1", 1, 0), ("2", 1, 1), ("3", 1, 2),
            ("4", 2, 0), ("5", 2, 1), ("6", 2, 2),
            ("7", 3, 0), ("8", 3, 1), ("9", 3, 2),
            ("CLR", 4, 0), ("0", 4, 1), ("⌫", 4, 2),
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
                self.tab_dialer,
                text=text,
                font=ctk.CTkFont(size=18, weight="bold"),
                fg_color=fg,
                hover_color=hover,
                command=cmd,
            )
            btn.grid(row=r, column=c, padx=5, pady=5, sticky="nsew")

        # Primary Action Button ("Find in Pi" <-> "Cancel")
        self.btn_action = ctk.CTkButton(
            self.tab_dialer,
            text="Find in Pi",
            fg_color="#2E7D32",
            hover_color="#1B5E20",
            font=ctk.CTkFont(size=16, weight="bold"),
            height=44,
            command=self._on_primary_action,
            state="disabled",
        )
        self.btn_action.grid(row=5, column=0, columnspan=3, padx=10, pady=(12, 8), sticky="ew")

        # Results & Flanking Context Display Container
        self.result_frame = ctk.CTkFrame(self.tab_dialer, fg_color="#1E1E1E", corner_radius=8)
        self.result_frame.grid(row=6, column=0, columnspan=3, padx=10, pady=(4, 10), sticky="ew")
        self.result_frame.grid_columnconfigure((0, 1, 2), weight=1)

        # Context labels with strict font hierarchy (14pt muted vs 28pt bold accent)
        context_container = ctk.CTkFrame(self.result_frame, fg_color="transparent")
        context_container.grid(row=0, column=0, columnspan=3, pady=(10, 4), padx=8)

        # Preceding 4 digits: Smaller, muted font (14pt gray)
        self.lbl_preceding = ctk.CTkLabel(
            context_container,
            text="",
            text_color="gray60",
            font=ctk.CTkFont(size=14),
        )
        self.lbl_preceding.pack(side="left", padx=4)

        # Target matched digits: Large, bold bright accent font (28pt bold)
        self.lbl_target = ctk.CTkLabel(
            context_container,
            text="—",
            text_color="#4CAF50",
            font=ctk.CTkFont(size=28, weight="bold"),
        )
        self.lbl_target.pack(side="left", padx=6)

        # Following 4 digits: Smaller, muted font (14pt gray)
        self.lbl_following = ctk.CTkLabel(
            context_container,
            text="",
            text_color="gray60",
            font=ctk.CTkFont(size=14),
        )
        self.lbl_following.pack(side="left", padx=4)

        # Telemetry & exact position info
        self.lbl_position = ctk.CTkLabel(
            self.result_frame,
            text="Enter a number and tap 'Find in Pi'",
            text_color="gray70",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.lbl_position.grid(row=1, column=0, columnspan=3, pady=(2, 2))

        self.lbl_telemetry = ctk.CTkLabel(
            self.result_frame,
            text="",
            text_color="gray50",
            font=ctk.CTkFont(size=11),
        )
        self.lbl_telemetry.grid(row=2, column=0, columnspan=3, pady=(0, 8))

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
        self.lbl_cached_banner.pack(padx=12, pady=10)

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
            card.pack(fill="x", padx=4, pady=4)
            card.grid_columnconfigure(0, weight=1)

            query = entry.get("query", "")
            pos = entry.get("position", 0)
            pre = entry.get("preceding", "")
            tgt = entry.get("target", query)
            flw = entry.get("following", "")
            source = entry.get("source", "Calculated")
            elapsed = entry.get("elapsed_seconds", 0.0)
            ts = entry.get("timestamp", "")

            # Title: Query & Position
            title_text = f"{query}  →  Digit #{pos:,}"
            lbl_title = ctk.CTkLabel(
                card,
                text=title_text,
                font=ctk.CTkFont(size=14, weight="bold"),
                text_color="#E0E0E0",
            )
            lbl_title.grid(row=0, column=0, sticky="w", padx=10, pady=(6, 0))

            # Context snippet: pre [tgt] flw
            context_text = f"Context: {pre} [{tgt}] {flw}"
            lbl_context = ctk.CTkLabel(
                card,
                text=context_text,
                text_color="#4CAF50",
                font=ctk.CTkFont(size=12),
            )
            lbl_context.grid(row=1, column=0, sticky="w", padx=10, pady=(0, 2))

            # Sub-info: Source, time, timestamp
            sub_info = f"{source} ({elapsed*1000:.1f}ms)  •  {ts}"
            lbl_sub = ctk.CTkLabel(
                card,
                text=sub_info,
                text_color="gray60",
                font=ctk.CTkFont(size=11),
            )
            lbl_sub.grid(row=2, column=0, sticky="w", padx=10, pady=(0, 6))

            # View button to reload this result into the Dialer tab
            btn_view = ctk.CTkButton(
                card,
                text="View",
                width=55,
                height=26,
                fg_color="#37474F",
                hover_color="#455A64",
                font=ctk.CTkFont(size=11, weight="bold"),
                command=lambda q=query, p=pos, pr=pre, t=tgt, f=flw, s=source, e=elapsed: self._load_history_result(
                    q, p, pr, t, f, s, e
                ),
            )
            btn_view.grid(row=0, column=1, rowspan=3, padx=10, pady=6, sticky="e")

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
        if not new_val.isdigit():
            return False
        if len(new_val) > 10:
            return False
        return True

    def _update_action_button_state(self):
        """Enables the 'Find in Pi' button only when input contains 1-10 digits."""
        if self._is_searching:
            return
        val = self.number_entry.get().strip()
        if 1 <= len(val) <= 10 and val.isdigit():
            self.btn_action.configure(state="normal")
        else:
            self.btn_action.configure(state="disabled")

    def _append_digit(self, char: str):
        """Appends a digit to the input entry if under 10 digits."""
        if self._is_searching:
            return
        current = self.number_entry.get()
        if len(current) < 10 and char.isdigit():
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
        elif keysym.startswith("KP_") and len(keysym) == 4 and keysym[3].isdigit():
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
        if not (1 <= len(query) <= 10 and query.isdigit()):
            return

        self._is_searching = True
        self._cancel_event.clear()

        # Update button into Cancel state
        self.btn_action.configure(
            text="Cancel Search",
            fg_color="#C62828",
            hover_color="#B71C1C",
            state="normal",
        )
        self.lbl_status.configure(text="● Searching...", text_color="#FFA000")
        self.lbl_preceding.configure(text="")
        self.lbl_target.configure(text=query)
        self.lbl_following.configure(text="")
        self.lbl_position.configure(text="Searching decimal expansion of Pi...")
        self.lbl_telemetry.configure(text="Querying index cache...")

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

            # Send checkpoint request with tier_config to Tkinter GUI thread
            self._msg_queue.put(
                (
                    "checkpoint",
                    (new_digits, total_cached, tier_config, response_event, response_holder),
                )
            )

            # Wait synchronously on worker thread until modal button clicked
            response_event.wait()
            return response_holder["action"]

        result = self.engine.search(
            query=query,
            cancel_event=self._cancel_event,
            progress_callback=progress_cb,
            checkpoint_callback=checkpoint_cb,
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

                    self.lbl_position.configure(text="Calculating Pi...")
                    if status_msg:
                        self.lbl_telemetry.configure(text=status_msg)
                    else:
                        self.lbl_telemetry.configure(
                            text=f"Calculating Pi... {digits_searched:,} digits searched ({elapsed:.1f}s elapsed)"
                        )
                elif msg_type == "checkpoint":
                    (
                        new_digits,
                        total_cached,
                        tier_config,
                        response_event,
                        response_holder,
                    ) = payload
                    self._show_checkpoint_modal(
                        new_digits,
                        total_cached,
                        tier_config,
                        response_event,
                        response_holder,
                    )
                elif msg_type == "found":
                    self._on_search_completed(payload)
                elif msg_type == "cancelled":
                    self._on_search_cancelled()
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
    ):
        """Opens the checkpoint pause modal with the 4-tier escalating schedule on the GUI thread."""
        def on_action(action: Any):
            response_holder["action"] = action
            response_event.set()
            if action == "stop":
                self._refresh_history_tab()

        CheckpointModal(
            self,
            new_digits,
            total_cached,
            tier_config,
            on_action,
        )

    def _on_search_completed(self, result: SearchResult):
        """Updates the dialer UI and history tab when a match is found."""
        self._is_searching = False
        self.btn_action.configure(
            text="Find in Pi",
            fg_color="#2E7D32",
            hover_color="#1B5E20",
        )
        self._update_action_button_state()

        self.lbl_status.configure(text="● Match Found", text_color="#4CAF50")
        self.lbl_preceding.configure(text=result.preceding)
        self.lbl_target.configure(text=result.target)
        self.lbl_following.configure(text=result.following)
        self.lbl_position.configure(text=f"Found at decimal digit #{result.position:,}")

        # Distinguish cache vs newly calculated in telemetry
        if result.new_digits_calculated > 0:
            source_desc = f"Source: {result.source} ({result.elapsed_seconds:.2f}s)"
        else:
            source_desc = f"Source: {result.source} ({result.elapsed_seconds * 1000:.1f}ms)"
        self.lbl_telemetry.configure(text=source_desc)

        # Refresh history tab content & total digits banner
        self._refresh_history_tab()

    def _on_search_cancelled(self):
        """Resets the dialer UI and updates cached count when calculation halts."""
        self._is_searching = False
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

        # Refresh history banner
        self._refresh_history_tab()


def main():
    app = PiPhoneApp()
    app.mainloop()


if __name__ == "__main__":
    main()