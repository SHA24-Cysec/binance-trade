from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

import pytest

import atomic_io
import procctl
import runtime_control as rc
import state
import pump_scanner_bot as pump_bot


def test_per_mode_lock_rejects_duplicate_live_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "ROOT", tmp_path)
    first = rc.BotModeLock("PAPER")
    second = rc.BotModeLock("PAPER")
    first.acquire()
    try:
        with pytest.raises(rc.BotAlreadyRunningError):
            second.acquire()
    finally:
        first.release()
    assert not rc.lock_file("PAPER").exists()


def test_partially_written_live_lock_is_not_stolen(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "ROOT", tmp_path)
    path = rc.lock_file("PAPER")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    result = []

    def contender():
        try:
            rc.BotModeLock("PAPER").acquire()
        except Exception as exc:  # hasil diperiksa di thread utama
            result.append(exc)

    thread = threading.Thread(target=contender)
    thread.start()
    time.sleep(0.05)
    payload = {
        "mode": "PAPER", "pid": os.getpid(),
        "process_identity": rc.procctl.process_identity(os.getpid()),
        "token": "owner-token", "created_at": time.time(),
    }
    os.write(fd, (json.dumps(payload) + "\n").encode("utf-8"))
    os.fsync(fd)
    os.close(fd)
    thread.join(timeout=2)
    try:
        assert not thread.is_alive()
        assert result and isinstance(result[0], rc.BotAlreadyRunningError)
        assert json.loads(path.read_text(encoding="utf-8"))["token"] == "owner-token"
    finally:
        path.unlink(missing_ok=True)


def test_stale_lock_is_archived_and_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "ROOT", tmp_path)
    lock_path = rc.lock_file("LIVE")
    lock_path.write_text(json.dumps({"pid": 99999999, "process_identity": "lama", "token": "stale-token"}), encoding="utf-8")
    monkeypatch.setattr(rc.procctl, "is_process_alive", lambda pid, identity=None: False)
    lock = rc.BotModeLock("LIVE")
    lock.acquire()
    try:
        assert json.loads(lock_path.read_text(encoding="utf-8"))["token"] == lock.token
        assert list(tmp_path.glob("pump_bot_lock_live.json.stale-*"))
    finally:
        lock.release()


def test_lifecycle_marks_exception_as_crashed(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "ROOT", tmp_path)
    monkeypatch.setattr(rc.procctl, "process_identity", lambda pid: "test-identity")
    with pytest.raises(RuntimeError):
        with rc.BotRuntime("PAPER"):
            raise RuntimeError("boom")
    data = json.loads(rc.process_file("PAPER").read_text(encoding="utf-8"))
    assert data["status"] == "CRASHED"
    assert data["exit_code"] == 1
    assert "RuntimeError" in data["reason"]
    assert not rc.lock_file("PAPER").exists()


def test_stop_request_is_separate_from_close_position(tmp_path):
    control = tmp_path / "control.json"
    state.save_control(control, {"action": "CLOSE_POSITION", "symbol": "ARBUSDT"})
    state.request_stop(control)
    assert state.load_control(control)["action"] == "CLOSE_POSITION"
    stop_file = Path(state.get_stop_control_file(control))
    assert stop_file.exists()
    assert state.consume_stop_request(control) is True
    assert not stop_file.exists()
    assert state.load_control(control)["action"] == "CLOSE_POSITION"


def test_wait_dead_treats_reused_pid_as_old_process_gone(monkeypatch):
    manager = rc.BotProcessManager()
    calls = []
    def alive(pid, identity=None):
        calls.append(identity)
        return identity is None
    monkeypatch.setattr(rc.procctl, "is_process_alive", alive)
    assert manager._wait_dead(12345, 0, "old-process-identity") is True
    assert calls == ["old-process-identity"]


def test_ws_backoff_resets_after_healthy_connection():
    from market_ws import MarketWebSocket

    assert MarketWebSocket._next_backoff(60.0, True) == 1.0
    assert MarketWebSocket._next_backoff(4.0, False) == 8.0


def test_supervisor_restart_budget_is_bounded(monkeypatch):
    import config as cfgmod

    monkeypatch.setitem(cfgmod.PUMP_CONFIG, "SUPERVISOR_AUTO_RESTART", True)
    monkeypatch.setitem(cfgmod.PUMP_CONFIG, "SUPERVISOR_MAX_RESTARTS", 2)
    monkeypatch.setitem(cfgmod.PUMP_CONFIG, "SUPERVISOR_RESTART_WINDOW_SECONDS", 300)
    monkeypatch.setitem(cfgmod.PUMP_CONFIG, "SUPERVISOR_RESTART_BACKOFF_SECONDS", 1)
    manager = rc.BotProcessManager()
    try:
        with manager._lock:
            manager._schedule_auto_restart_locked("PAPER")
            manager._schedule_auto_restart_locked("PAPER")
            manager._schedule_auto_restart_locked("PAPER")
        assert manager._restart_attempts == 2
        assert manager._auto_restart_mode is None
        assert "dihentikan" in (manager._last_job_warning or "").lower()
    finally:
        manager._watchdog_stop.set()


def test_signal_handler_wakes_interruptible_shutdown_wait():
    pump_bot._shutdown_event.clear()
    pump_bot._shutdown_requested = False
    pump_bot._handle_signal(15, None)
    assert pump_bot._shutdown_requested is True
    assert pump_bot._shutdown_event.is_set()
    pump_bot._shutdown_event.clear()
    pump_bot._shutdown_requested = False


def test_replace_retries_permission_error(monkeypatch, tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_text("baru", encoding="utf-8")
    original = os.replace
    calls = {"count": 0}

    def flaky(src, dst):
        calls["count"] += 1
        if calls["count"] < 3:
            raise PermissionError("sharing violation")
        return original(src, dst)

    monkeypatch.setattr(atomic_io.os, "replace", flaky)
    monkeypatch.setattr(atomic_io.time, "sleep", lambda delay: None)
    atomic_io.replace_with_retry(source, target, delays=(0, 0, 0))
    assert calls["count"] == 3
    assert target.read_text(encoding="utf-8") == "baru"


@pytest.mark.skipif(os.name != "posix", reason="Sinyal POSIX hanya diuji di Linux/POSIX")
def test_posix_process_identity_for_current_process_is_stable():
    identity = procctl.process_identity(os.getpid())
    assert identity
    assert procctl.is_process_alive(os.getpid(), identity)


@pytest.mark.skipif(os.name != "nt", reason="CTRL_BREAK_EVENT hanya tersedia di Windows")
def test_windows_process_creation_flag_is_available():
    import subprocess
    assert hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP")
    assert hasattr(__import__("signal"), "CTRL_BREAK_EVENT")
