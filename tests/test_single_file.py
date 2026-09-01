"""Single-file realtime + SaveAnywhere removal tests.

Covers the change: Planner now only tracks File_1; SaveAnywhere_* folders
are ignored even in legacy scan. TR_SINGLE_FILE env toggles behaviour.
"""
import os
import time
import tempfile
import pathlib
import pytest

from planner.parser.saves import discover_slots, get_slot, latest_save_in_folder


def _make_slot(root: str, slot_name: str, filenames: list[str] = None):
    """Helper: create a fake save slot folder with .save files."""
    if filenames is None:
        filenames = ["SaveFile-1-1-2026-0-0-1.save"]
    slot_dir = os.path.join(root, slot_name)
    os.makedirs(slot_dir, exist_ok=True)
    for fn in filenames:
        p = os.path.join(slot_dir, fn)
        pathlib.Path(p).write_bytes(b"fake save content " + os.urandom(8))
        # stagger mtime so latest is deterministic
        time.sleep(0.02)
    return slot_dir


def test_single_file_only_by_default_filters_to_File_1(tmp_path):
    root = str(tmp_path)
    _make_slot(root, "File_1", ["SaveFile-1-1-2026-0-0-1.save"])
    _make_slot(root, "File_2", ["SaveFile-1-1-2026-0-0-2.save"])
    _make_slot(root, "SaveAnywhere_Manual_1", ["SaveFile-1-1-2026-0-0-3.save"])
    _make_slot(root, "SaveAnywhere_Auto_1", ["SaveFile-1-1-2026-0-0-4.save"])
    slots = discover_slots(root)
    ids = [s.slot_id for s in slots]
    assert ids == ["File_1"], f"single-file mode should only return File_1, got {ids}"
    # ensure SaveAnywhere never leaks
    assert all("SaveAnywhere" not in s for s in ids)


def test_single_file_false_scans_all_File_slots(tmp_path):
    root = str(tmp_path)
    _make_slot(root, "File_1")
    _make_slot(root, "File_2")
    _make_slot(root, "File_3")
    _make_slot(root, "SaveAnywhere_Manual_1")  # should still be ignored
    slots = discover_slots(root, single_file_only=False)
    ids = {s.slot_id for s in slots}
    assert ids == {"File_1", "File_2", "File_3"}
    assert "SaveAnywhere_Manual_1" not in ids


def test_saveanywhere_always_ignored_even_in_legacy_mode(tmp_path):
    root = str(tmp_path)
    for name in ["SaveAnywhere_Manual_1", "SaveAnywhere_Auto_1", "SaveAnywhere_Manual_2"]:
        _make_slot(root, name)
    _make_slot(root, "File_1")
    # legacy mode scans File_* but still ignores SaveAnywhere
    slots = discover_slots(root, single_file_only=False)
    assert all("SaveAnywhere" not in s.slot_id for s in slots)
    assert len(slots) == 1


def test_env_TR_SINGLE_FILE_disables_single_mode(monkeypatch, tmp_path):
    root = str(tmp_path)
    _make_slot(root, "File_1")
    _make_slot(root, "File_2")
    monkeypatch.setenv("TR_SINGLE_FILE", "0")
    slots = discover_slots(root, single_file_only=True)
    # env override flips to legacy behaviour
    ids = {s.slot_id for s in slots}
    assert ids == {"File_1", "File_2"}
    monkeypatch.delenv("TR_SINGLE_FILE", raising=False)
    # after del, back to single-file
    slots2 = discover_slots(root, single_file_only=True)
    assert [s.slot_id for s in slots2] == ["File_1"]


def test_discover_slots_empty_root(tmp_path):
    root = str(tmp_path / "empty")
    os.makedirs(root)
    assert discover_slots(root) == []
    assert discover_slots("/nonexistent/path/that/does/not/exist") == []


def test_discover_slots_ignores_non_save_folder(tmp_path):
    root = str(tmp_path)
    _make_slot(root, "File_1")
    os.makedirs(os.path.join(root, "NotASave"))
    pathlib.Path(os.path.join(root, "random.txt")).write_text("hi")
    pathlib.Path(os.path.join(root, "File_1/Save.backup")).write_text("backup")
    slots = discover_slots(root)
    assert len(slots) == 1
    assert slots[0].slot_id == "File_1"


def test_discover_slots_label_contains_slot_id(tmp_path):
    root = str(tmp_path)
    _make_slot(root, "File_1")
    slots = discover_slots(root)
    assert "File_1" in slots[0].label


def test_discover_slots_empty_folder_no_saves(tmp_path):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "File_1"))
    # folder exists but no SaveFile*.save
    assert discover_slots(root) == []


def test_get_slot_returns_File_1_single_mode(tmp_path, monkeypatch):
    root = str(tmp_path)
    _make_slot(root, "File_1")
    _make_slot(root, "File_2")
    monkeypatch.setenv("TR_SAVES_DIR", root)
    # single_file_only default, get_slot(None) should return newest File_1 only
    s = get_slot(None)
    assert s is not None
    assert s.slot_id == "File_1"
    # explicit File_2 is not tracked in single-file mode — falls back to newest (File_1)
    fallback = get_slot("File_2")
    assert fallback is not None
    assert fallback.slot_id == "File_1"
    monkeypatch.delenv("TR_SAVES_DIR", raising=False)


def test_latest_save_in_folder_picks_most_recent(tmp_path):
    root = str(tmp_path)
    slot_dir = _make_slot(root, "File_1", ["SaveFile-a.save", "SaveFile-b.save", "SaveFile-c.save"])
    latest = latest_save_in_folder(slot_dir)
    assert latest is not None
    assert latest.endswith("SaveFile-c.save")
    # empty folder
    empty = os.path.join(root, "Empty")
    os.makedirs(empty)
    assert latest_save_in_folder(empty) is None
    assert latest_save_in_folder("/nonexistent") is None


def test_real_saves_root_respects_env(monkeypatch, tmp_path):
    fake_root = str(tmp_path / "MySaves")
    os.makedirs(fake_root)
    _make_slot(fake_root, "File_1")
    monkeypatch.setenv("TR_SAVES_DIR", fake_root)
    from planner.parser.saves import saves_root
    assert saves_root() == fake_root
    monkeypatch.delenv("TR_SAVES_DIR", raising=False)


def test_discover_slots_sorted_newest_first(tmp_path):
    root = str(tmp_path)
    # File_1 will be older than File_2 when single_file disabled
    _make_slot(root, "File_1")
    time.sleep(0.05)
    _make_slot(root, "File_2")
    slots = discover_slots(root, single_file_only=False)
    # File_2 should be first (newer mtime)
    assert slots[0].slot_id == "File_2"
    assert slots[1].slot_id == "File_1"
