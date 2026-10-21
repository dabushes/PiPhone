"""
Unit tests for PiPhone UI components:
- ResultDisplayManager & _ResultPartProxy (interactive, selectable tk.Text result display)
- ReadonlySelectableEntry (borderless, selectable read-only entry with configure(text=...) support)
- Keyboard shortcut Ctrl+C / Ctrl+A bypass verification
"""

import tkinter as tk
from unittest.mock import MagicMock
import pytest

from piphone import ResultDisplayManager, _ResultPartProxy, ReadonlySelectableEntry, PiPhoneApp


@pytest.fixture(scope="module")
def root_tk():
    """Module-level Tk instance for headless widget testing."""
    root = tk.Tk()
    root.withdraw()
    yield root
    try:
        root.destroy()
    except Exception:
        pass


def test_readonly_selectable_entry(root_tk):
    """Verifies that ReadonlySelectableEntry is read-only, supports configure(text=...), and allows selection."""
    entry = ReadonlySelectableEntry(root_tk, text="Initial Text")
    assert entry.get() == "Initial Text"
    assert entry.cget("state") == "readonly"
    assert entry.cget("text") == "Initial Text"

    # Update text via configure()
    entry.configure(text="Updated Telemetry #123")
    assert entry.get() == "Updated Telemetry #123"
    assert entry.cget("state") == "readonly"

    # Verify selection can be made
    entry.select_range(0, 7)
    assert entry.selection_present() is True


def test_result_display_manager_and_proxies(root_tk):
    """Verifies ResultDisplayManager formats context and target parts correctly with tags."""
    txt = tk.Text(root_tk)
    manager = ResultDisplayManager(txt)

    lbl_preceding = _ResultPartProxy(manager, "preceding")
    lbl_target = _ResultPartProxy(manager, "target")
    lbl_following = _ResultPartProxy(manager, "following")

    # Initial state
    assert "—" in txt.get("1.0", "end")

    # Set match results via proxies
    lbl_preceding.configure(text="...3.14")
    lbl_target.configure(text="1592")
    lbl_following.configure(text="6535...")

    content = txt.get("1.0", "end").strip()
    assert content == "...3.14 1592 6535..."

    # Verify tags exist in the text widget
    tags = txt.tag_names()
    assert "center" in tags
    assert "context" in tags
    assert "target" in tags


def test_key_press_ctrl_bypass():
    """Verifies that PiPhoneApp._on_key_press bypasses execution when Control is held (event.state & 4)."""
    mock_app = MagicMock(spec=PiPhoneApp)
    # Re-bind the real method to the mock
    mock_app._on_key_press = PiPhoneApp._on_key_press.__get__(mock_app, PiPhoneApp)

    # Simulate Ctrl+C (event.state = 4, keysym = 'c')
    event_ctrl_c = MagicMock()
    event_ctrl_c.state = 4
    event_ctrl_c.keysym = "c"

    res = mock_app._on_key_press(event_ctrl_c)
    assert res is None  # Should cleanly return without calling dialer actions
    mock_app._on_primary_action.assert_not_called()
    mock_app._append_digit.assert_not_called()
    mock_app._clear_input.assert_not_called()


def test_aria_announcement_label(root_tk):
    """Verifies AriaAnnouncementLabel updates text with screen reader announcement."""
    from piphone import AriaAnnouncementLabel
    label = AriaAnnouncementLabel(root_tk)
    label.announce("Search completed: sequence 12345 found at position 500.")
    assert "Search completed" in label.cget("text")
    assert "12345" in label.cget("text")

