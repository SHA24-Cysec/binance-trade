from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

import procctl
import runtime_control as rc
import state


@pytest.mark.skipif(os.name != "posix", reason="E2E lifecycle ini dijalankan di Linux/POSIX")
def test_dashboard_manager_start_restart_stop_paper_without_live_orders(tmp_path, monkeypatch):
    control = tmp_path / "control-paper.json"
    cfg = SimpleNamespace()
    cfg.PUMP_CONFIG = {
        "MODE": "PAPER",
        "API_KEY": "",
        "API_SECRET": "",
        "CONTROL_FILE": str(control),
        "STATE_FILE": str(tmp_path / "state-paper.json"),
        "LOOP_INTERVAL_SECONDS": 1,
    }
    cfg.CONFIG_LOAD_ERRORS = []
    cfg.get_mode = lambda data=None: "PAPER"
    cfg.require_valid_mode = lambda data=None: "PAPER"
    cfg.reload_config = lambda: cfg.PUMP_CONFIG

    stop_file = state.get_stop_control_file(str(control))
    helper = (
        "import os,time\n"
        f"p={stop_file!r}\n"
        "while not os.path.exists(p): time.sleep(0.02)\n"
    )

    def fake_spawn(script, *, args=None, cwd=None, env=None):
        proc = subprocess.Popen([sys.executable, "-c", helper], cwd=tmp_path, env=env,
                                start_new_session=True)
        return proc, procctl.ProcessTreeHandle(proc)

    monkeypatch.setattr(rc, "ROOT", tmp_path)
    monkeypatch.setattr(rc.procctl, "spawn_python", fake_spawn)
    manager = rc.BotProcessManager()
    monkeypatch.setattr(manager, "_config", lambda: cfg)

    started = manager.start()
    first_pid = started["pid"]
    assert started["mode"] == "PAPER"
    assert started["status"] in {"STARTING", "RUNNING"}
    assert procctl.is_process_alive(first_pid)

    restarted = manager.restart(position_policy="REQUIRE_EMPTY")
    second_pid = restarted["pid"]
    assert restarted["status"] in {"STARTING", "RUNNING"}
    assert second_pid != first_pid

    stopped = manager.stop(position_policy="REQUIRE_EMPTY", graceful_timeout=3, signal_timeout=1)
    assert stopped["status"] == "STOPPED"
    assert stopped["exit_code"] == 0
    assert manager._proc is None
    # Pengujian tidak pernah membangun konfigurasi LIVE atau mengirim order.
    assert cfg.PUMP_CONFIG["MODE"] == "PAPER"
