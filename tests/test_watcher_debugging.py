"""SaveWatcher realtime single-file + debugging tests.

Validates the watcher that was rewritten for single-file mode:
 - 200ms debounce (not old 500ms)
 - ignores SaveAnywhere paths even if emitted
 - broadcast payload includes graphical-feedback fields
"""
import asyncio
import time
import os
import tempfile
import pathlib
import pytest
from unittest.mock import MagicMock, patch

from planner.server.app import SaveWatcher, manager


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def _make_event(src_path, is_dir=False):
    m = type("E", (), {"src_path": src_path, "is_directory": is_dir, "dest_path": src_path})()
    return m


def _capture_broadcast(event_loop, watcher, path, reason="test"):
    """Helper that forces SaveWatcher._emit to synchronously capture broadcast msg."""
    captured = []

    async def fake_broadcast(msg):
        captured.append(msg)

    with patch.object(manager, "broadcast", side_effect=fake_broadcast):
        # make run_coroutine_threadsafe execute immediately instead of scheduling
        def fake_run(coro, loop):
            # run the coro to completion so fake_broadcast appends
            try:
                loop.run_until_complete(coro)
            except RuntimeError:
                # loop already running fallback
                import asyncio as _a
                _a.run(coro)
            return MagicMock()

        with patch("planner.server.app.asyncio.run_coroutine_threadsafe", side_effect=fake_run):
            watcher._emit(path, reason=reason)
    return captured


def test_watcher_ignores_saveanywhere_modified(event_loop):
    w = SaveWatcher(event_loop)
    captured = []

    async def fake_broadcast(msg):
        captured.append(msg)

    def fake_run(coro, loop):
        try:
            loop.run_until_complete(coro)
        except Exception:
            pass
        return MagicMock()

    with patch.object(manager, "broadcast", side_effect=fake_broadcast):
        with patch("planner.server.app.asyncio.run_coroutine_threadsafe", side_effect=fake_run):
            ev = _make_event("/tmp/GameSaves/SaveAnywhere_Manual_1/SaveFile-1.save")
            # SaveAnywhere path should be filtered even if we force _emit, but on_modified filters before _emit
            w.on_modified(ev)
            time.sleep(0.05)
            assert len(captured) == 0, "SaveAnywhere should be ignored in single-file mode"


def test_watcher_debounce_200ms(event_loop):
    w = SaveWatcher(event_loop)
    captured = []

    async def fake_broadcast(msg):
        captured.append(msg)

    def fake_run(coro, loop):
        try:
            loop.run_until_complete(coro)
        except Exception:
            pass
        return MagicMock()

    with patch.object(manager, "broadcast", side_effect=fake_broadcast):
        with patch("planner.server.app.asyncio.run_coroutine_threadsafe", side_effect=fake_run):
            with tempfile.TemporaryDirectory() as td:
                fp = os.path.join(td, "File_1", "SaveFile-1.save")
                os.makedirs(os.path.join(td, "File_1"), exist_ok=True)
                pathlib.Path(fp).write_bytes(b"abc")
                w._emit(fp, reason="first")
                assert len(captured) == 1
                w._emit(fp, reason="second-fast")
                assert len(captured) == 1, "second within 200ms should be debounced"
                time.sleep(0.25)
                w._emit(fp, reason="third-slow")
                assert len(captured) == 2
                assert captured[1]["reason"] == "third-slow"


def test_watcher_ignores_directories(event_loop):
    w = SaveWatcher(event_loop)
    captured = []

    async def fake_broadcast(msg):
        captured.append(msg)

    def fake_run(coro, loop):
        try:
            loop.run_until_complete(coro)
        except Exception:
            pass
        return MagicMock()

    with patch.object(manager, "broadcast", side_effect=fake_broadcast):
        with patch("planner.server.app.asyncio.run_coroutine_threadsafe", side_effect=fake_run):
            w.on_modified(_make_event("/tmp/File_1", is_dir=True))
            w.on_created(_make_event("/tmp/File_1", is_dir=True))
            w.on_moved(_make_event("/tmp/File_1", is_dir=True))
            assert len(captured) == 0


def test_watcher_ignores_non_save_extension(event_loop):
    w = SaveWatcher(event_loop)
    captured = []

    async def fake_broadcast(msg):
        captured.append(msg)

    def fake_run(coro, loop):
        try:
            loop.run_until_complete(coro)
        except Exception:
            pass
        return MagicMock()

    with patch.object(manager, "broadcast", side_effect=fake_broadcast):
        with patch("planner.server.app.asyncio.run_coroutine_threadsafe", side_effect=fake_run):
            with tempfile.TemporaryDirectory() as td:
                fp = os.path.join(td, "File_1", "Save.backup")
                os.makedirs(os.path.join(td, "File_1"), exist_ok=True)
                pathlib.Path(fp).write_bytes(b"x")
                w.on_modified(_make_event(fp))
                w.on_created(_make_event(fp))
                assert len(captured) == 0


def test_watcher_on_created_and_moved_emit(event_loop):
    w = SaveWatcher(event_loop)
    captured = []

    async def fake_broadcast(msg):
        captured.append(msg)

    def fake_run(coro, loop):
        try:
            loop.run_until_complete(coro)
        except Exception:
            pass
        return MagicMock()

    with patch.object(manager, "broadcast", side_effect=fake_broadcast):
        with patch("planner.server.app.asyncio.run_coroutine_threadsafe", side_effect=fake_run):
            with tempfile.TemporaryDirectory() as td:
                fp = os.path.join(td, "File_1", "SaveFile-new.save")
                os.makedirs(os.path.join(td, "File_1"), exist_ok=True)
                pathlib.Path(fp).write_bytes(b"hi")
                w._last_emit_at = 0
                w.on_created(_make_event(fp))
                assert len(captured) == 1
                assert captured[0]["reason"] == "created"
                captured.clear()
                time.sleep(0.25)
                ev = type("E", (), {"is_directory": False, "dest_path": fp, "src_path": fp})()
                w.on_moved(ev)
                assert len(captured) == 1
                assert captured[0]["reason"] == "moved"


def test_watcher_payload_graphical_feedback_fields(event_loop):
    w = SaveWatcher(event_loop)
    captured = []

    async def fake_broadcast(msg):
        captured.append(msg)

    def fake_run(coro, loop):
        try:
            loop.run_until_complete(coro)
        except Exception:
            pass
        return MagicMock()

    with patch.object(manager, "broadcast", side_effect=fake_broadcast):
        with patch("planner.server.app.asyncio.run_coroutine_threadsafe", side_effect=fake_run):
            with tempfile.TemporaryDirectory() as td:
                fp = os.path.join(td, "File_1", "SaveFile-graphical.save")
                os.makedirs(os.path.join(td, "File_1"), exist_ok=True)
                pathlib.Path(fp).write_bytes(b"12" * 500)
                w._last_emit_at = 0
                w._emit(fp, reason="graphical-test")
                assert len(captured) == 1
                p = captured[0]
                assert p["type"] == "save_changed"
                assert p["slot"] == "File_1"
                assert p["single_file"] is True
                assert p["reason"] == "graphical-test"
                assert isinstance(p["size"], int) and p["size"] >= 1000
                assert isinstance(p["mtime"], float)
                assert "File_1" in p["path"]
