"""Lock bot, lifecycle status, dan pengelola subprocess dashboard."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from atomic_io import atomic_write_json, read_json, replace_with_retry, timestamp_tag
import procctl
import state as state_mod


ROOT = Path(__file__).resolve().parent


class BotAlreadyRunningError(RuntimeError):
    pass


class BotControlError(RuntimeError):
    pass


def _mode(mode: str) -> str:
    raw = str(mode).strip().upper()
    if raw not in ("PAPER", "LIVE"):
        raise ValueError(f"Mode tidak valid: {mode!r}")
    return raw


def lock_file(mode: str) -> Path:
    return ROOT / f"pump_bot_lock_{_mode(mode).lower()}.json"


def process_file(mode: str) -> Path:
    return ROOT / f"pump_bot_process_{_mode(mode).lower()}.json"


def reclaim_lock_file(mode: str) -> Path:
    return ROOT / f"pump_bot_lock_{_mode(mode).lower()}.reclaim"


def _read_dict(path: Path) -> dict:
    try:
        data = read_json(path, {})
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return {}


def _archive_stale(path: Path) -> None:
    if not path.exists():
        return
    target = path.with_name(f"{path.name}.stale-{timestamp_tag()}-{uuid.uuid4().hex[:6]}")
    try:
        replace_with_retry(path, target)
    except FileNotFoundError:
        pass


def lock_owner(mode: str) -> dict:
    data = _read_dict(lock_file(mode))
    if not data:
        return {}
    try:
        pid = int(data.get("pid", 0))
    except (TypeError, ValueError):
        return {}
    if procctl.is_process_alive(pid, data.get("process_identity")):
        return data
    return {}


class BotModeLock:
    """Lock file O_EXCL per mode dengan verifikasi PID dan waktu proses."""

    def __init__(self, mode: str):
        self.mode = _mode(mode)
        self.path = lock_file(self.mode)
        self.token = uuid.uuid4().hex
        self.acquired = False

    def acquire(self) -> None:
        payload = {
            "mode": self.mode,
            "pid": os.getpid(),
            "process_identity": procctl.process_identity(os.getpid()),
            "token": self.token,
            "created_at": time.time(),
        }
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        reclaim_path = reclaim_lock_file(self.mode)
        for _ in range(150):
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                existing = _read_dict(self.path)
                try:
                    pid = int(existing.get("pid", 0))
                except (TypeError, ValueError):
                    pid = 0
                token = existing.get("token")
                if pid and procctl.is_process_alive(pid, existing.get("process_identity")):
                    raise BotAlreadyRunningError(
                        f"Bot mode {self.mode} sudah berjalan dengan PID {pid}."
                    )

                # File baru dapat terlihat sesaat sebelum payload selesai
                # ditulis. Jangan menganggap lock kosong itu stale selama
                # grace period, karena hal itu dapat menghasilkan dua bot.
                if not pid or not token:
                    try:
                        age = max(0.0, time.time() - self.path.stat().st_mtime)
                    except FileNotFoundError:
                        continue
                    if age < 2.0:
                        time.sleep(0.02)
                        continue

                # Hanya satu proses boleh merebut lock stale. Setelah claim
                # didapat, baca ulang dan cocokkan token agar lock owner baru
                # tidak pernah dipindahkan karena hasil baca lama (TOCTOU).
                try:
                    reclaim_fd = os.open(
                        reclaim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                    )
                except FileExistsError:
                    try:
                        reclaim_age = max(0.0, time.time() - reclaim_path.stat().st_mtime)
                        if reclaim_age > 2.0:
                            reclaim_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    time.sleep(0.02)
                    continue
                try:
                    os.close(reclaim_fd)
                    current = _read_dict(self.path)
                    if current.get("token") != token or current.get("pid") != existing.get("pid"):
                        continue
                    try:
                        current_pid = int(current.get("pid", 0))
                    except (TypeError, ValueError):
                        current_pid = 0
                    if current_pid and procctl.is_process_alive(
                        current_pid, current.get("process_identity")
                    ):
                        raise BotAlreadyRunningError(
                            f"Bot mode {self.mode} sudah berjalan dengan PID {current_pid}."
                        )
                    _archive_stale(self.path)
                finally:
                    try:
                        reclaim_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                continue
            try:
                os.write(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)
            self.acquired = True
            return
        raise BotControlError(f"Gagal mengambil lock bot {self.mode} setelah retry.")

    def release(self) -> None:
        if not self.acquired:
            return
        existing = _read_dict(self.path)
        if existing.get("token") == self.token:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
        self.acquired = False


class BotLifecycle:
    """Penulis status proses dari dalam proses bot."""

    def __init__(self, mode: str, managed: bool | None = None):
        self.mode = _mode(mode)
        self.path = process_file(self.mode)
        self.started_at = time.time()
        self.pid = os.getpid()
        self.identity = procctl.process_identity(self.pid)
        if managed is None:
            managed = os.environ.get("PUMP_BOT_MANAGED", "") == "1"
        self.managed = bool(managed)
        self.last_status = "STARTING"

    def write(self, status: str, *, exit_code: int | None = None,
              reason: str | None = None, extra: dict | None = None) -> None:
        self.last_status = status
        data = {
            "mode": self.mode,
            "status": status,
            "pid": self.pid,
            "process_identity": self.identity,
            "managed": self.managed,
            "started_at": self.started_at,
            "updated_at": time.time(),
            "exit_code": exit_code,
            "reason": reason,
        }
        if extra:
            data.update(extra)
        atomic_write_json(self.path, data)

    def heartbeat(self, status: str | None = None) -> None:
        self.write(status or self.last_status)


class BotRuntime:
    """Context proses bot yang memastikan lock dan status selalu diperbarui."""

    def __init__(self, mode: str):
        self.mode = _mode(mode)
        self.lock = BotModeLock(self.mode)
        self.lifecycle = BotLifecycle(self.mode)
        self._finished = False

    def __enter__(self) -> BotLifecycle:
        self.lock.acquire()
        self.lifecycle.write("STARTING")
        return self.lifecycle

    def finish(self, code: int, reason: str | None = None) -> None:
        status = "STOPPED" if int(code) == 0 else "CRASHED"
        self.lifecycle.write(status, exit_code=int(code), reason=reason)
        self._finished = True

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if not self._finished:
                if exc is None:
                    self.lifecycle.write("STOPPED", exit_code=0)
                else:
                    self.lifecycle.write("CRASHED", exit_code=1,
                                         reason=f"{type(exc).__name__}: {str(exc)[:300]}")
        finally:
            self.lock.release()
        return False


class BotProcessManager:
    """Satu pemilik subprocess bot di dalam proses dashboard."""

    def __init__(self):
        self._lock = threading.RLock()
        self._proc = None
        self._tree: procctl.ProcessTreeHandle | None = None
        self._managed_mode: str | None = None
        self._last_job_warning: str | None = None
        self._intentional_stop = False
        self._auto_restart_mode: str | None = None
        self._restart_attempts = 0
        self._restart_window_started = 0.0
        self._restart_next_at = 0.0
        self._watchdog_stop = threading.Event()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, name="bot-supervisor", daemon=True
        )
        self._watchdog.start()

    @staticmethod
    def _config():
        import config
        return config

    def _read_lifecycle(self, mode: str) -> dict:
        return _read_dict(process_file(mode))

    def _managed_poll(self) -> int | None:
        if self._proc is None:
            return None
        return self._proc.poll()

    def _schedule_auto_restart_locked(self, mode: str) -> None:
        cfgmod = self._config()
        cfg = cfgmod.PUMP_CONFIG
        if not bool(cfg.get("SUPERVISOR_AUTO_RESTART", False)):
            return
        now = time.monotonic()
        window = max(60.0, float(cfg.get("SUPERVISOR_RESTART_WINDOW_SECONDS", 300) or 300))
        if now - self._restart_window_started >= window:
            self._restart_window_started = now
            self._restart_attempts = 0
        maximum = max(0, int(cfg.get("SUPERVISOR_MAX_RESTARTS", 5) or 5))
        if self._restart_attempts >= maximum:
            self._auto_restart_mode = None
            self._last_job_warning = (
                f"Auto-restart dihentikan setelah {maximum} percobaan dalam {window:.0f} detik."
            )
            return
        base = max(1.0, float(cfg.get("SUPERVISOR_RESTART_BACKOFF_SECONDS", 5) or 5))
        delay = min(base * (2 ** self._restart_attempts), window)
        self._restart_attempts += 1
        self._auto_restart_mode = mode
        self._restart_next_at = now + delay
        self._last_job_warning = (
            f"Bot {mode} crash; auto-restart {self._restart_attempts}/{maximum} "
            f"dalam {delay:.1f} detik."
        )

    def _handle_managed_exit_locked(self, mode: str, code: int) -> None:
        intentional = self._intentional_stop
        lifecycle = self._read_lifecycle(mode)
        self._record_managed_exit(mode, int(code), lifecycle)
        self._clear_managed_handles()
        if not intentional:
            self._schedule_auto_restart_locked(mode)

    def _watchdog_loop(self) -> None:
        """Pantau child tanpa menunggu request dashboard berikutnya.

        Restart hanya untuk crash tidak terduga, memakai exponential backoff
        dan budget per window agar exception deterministik tidak menjadi loop
        restart tanpa akhir.
        """
        while not self._watchdog_stop.wait(0.25):
            restart = False
            with self._lock:
                if self._proc is not None:
                    code = self._proc.poll()
                    if code is not None:
                        mode = self._managed_mode
                        if mode:
                            self._handle_managed_exit_locked(mode, int(code))
                mode = self._auto_restart_mode
                if (
                    mode
                    and self._proc is None
                    and not self._intentional_stop
                    and time.monotonic() >= self._restart_next_at
                ):
                    cfgmod = self._config()
                    if mode != cfgmod.get_mode(cfgmod.PUMP_CONFIG):
                        self._auto_restart_mode = None
                    else:
                        self._auto_restart_mode = None
                        restart = True
            if restart:
                try:
                    self.start(_auto_restart=True)
                except (BotControlError, ValueError) as exc:
                    with self._lock:
                        self._last_job_warning = f"Auto-restart gagal: {exc}"
                        self._schedule_auto_restart_locked(mode)

    def status(self, mode: str | None = None) -> dict:
        cfgmod = self._config()
        mode = _mode(mode or cfgmod.get_mode(cfgmod.PUMP_CONFIG))
        with self._lock:
            lifecycle = self._read_lifecycle(mode)
            proc = self._proc if self._managed_mode == mode else None
            if proc is not None:
                code = proc.poll()
                if code is None:
                    pid = proc.pid
                    status = lifecycle.get("status") or "STARTING"
                    if status not in ("STARTING", "RUNNING", "STOPPING"):
                        status = "RUNNING"
                    started = float(lifecycle.get("started_at") or time.time())
                    return self._status_payload(mode, status, pid, started, None,
                                                lifecycle.get("reason"), True, lifecycle)
                self._handle_managed_exit_locked(mode, int(code))
                lifecycle = self._read_lifecycle(mode)

            owner = lock_owner(mode)
            if owner:
                pid = int(owner["pid"])
                started = float(lifecycle.get("started_at") or owner.get("created_at") or time.time())
                status = lifecycle.get("status") or "RUNNING"
                if status not in ("STARTING", "RUNNING", "STOPPING"):
                    status = "RUNNING"
                owner_lifecycle = dict(lifecycle)
                owner_lifecycle.setdefault("process_identity", owner.get("process_identity"))
                return self._status_payload(mode, status, pid, started, None,
                                            lifecycle.get("reason"), bool(lifecycle.get("managed")), owner_lifecycle)

            status = str(lifecycle.get("status") or "STOPPED")
            if status in ("STARTING", "RUNNING", "STOPPING"):
                status = "CRASHED"
            return self._status_payload(
                mode, status, lifecycle.get("pid"), lifecycle.get("started_at"),
                lifecycle.get("exit_code"), lifecycle.get("reason"),
                bool(lifecycle.get("managed")), lifecycle,
            )

    def _status_payload(self, mode: str, status: str, pid: Any, started: Any,
                        exit_code: Any, reason: Any, managed: bool, lifecycle: dict) -> dict:
        now = time.time()
        try:
            uptime = max(0.0, now - float(started)) if started and status in ("STARTING", "RUNNING", "STOPPING") else None
        except (TypeError, ValueError):
            uptime = None
        last_pid = int(pid) if pid else None
        active = status in ("STARTING", "RUNNING", "STOPPING")
        return {
            "mode": mode,
            "status": status,
            "pid": last_pid if active else None,
            "last_pid": last_pid,
            "process_identity": lifecycle.get("process_identity"),
            "uptime_seconds": uptime,
            "started_at": started,
            "exit_code": exit_code,
            "reason": reason,
            "managed_by_dashboard": managed,
            "last_heartbeat": lifecycle.get("updated_at"),
            "job_tree_protected": bool(self._tree and self._tree.active),
            "job_warning": self._last_job_warning,
        }

    def _record_managed_exit(self, mode: str, code: int, lifecycle: dict | None = None) -> None:
        lifecycle = lifecycle or {}
        status = "STOPPED" if code == 0 else "CRASHED"
        current = self._read_lifecycle(mode)
        if current.get("status") == "STOPPED" and code == 0:
            return
        data = {
            "mode": mode,
            "status": status,
            "pid": getattr(self._proc, "pid", current.get("pid")),
            "process_identity": current.get("process_identity"),
            "managed": True,
            "started_at": current.get("started_at") or lifecycle.get("started_at"),
            "updated_at": time.time(),
            "exit_code": code,
            "reason": current.get("reason") or (None if code == 0 else "Proses bot keluar tidak terduga."),
        }
        atomic_write_json(process_file(mode), data)

    def _clear_managed_handles(self) -> None:
        if self._tree is not None:
            self._tree.close()
        self._tree = None
        self._proc = None
        self._managed_mode = None

    def start(self, *, _auto_restart: bool = False) -> dict:
        cfgmod = self._config()
        with self._lock:
            self._intentional_stop = False
            self._auto_restart_mode = None
            if not _auto_restart:
                self._restart_attempts = 0
                self._restart_window_started = time.monotonic()
            if cfgmod.CONFIG_LOAD_ERRORS:
                raise BotControlError("Konfigurasi runtime rusak: " + "; ".join(cfgmod.CONFIG_LOAD_ERRORS))
            mode = cfgmod.require_valid_mode(cfgmod.PUMP_CONFIG)
            current = self.status(mode)
            if current["status"] in ("STARTING", "RUNNING", "STOPPING"):
                raise BotControlError(f"Bot sudah {current['status']} dengan PID {current['pid']}.")
            if mode == "LIVE" and (not cfgmod.PUMP_CONFIG.get("API_KEY") or not cfgmod.PUMP_CONFIG.get("API_SECRET")):
                raise BotControlError("Kredensial LIVE belum lengkap.")

            state_mod.clear_stop_request(cfgmod.PUMP_CONFIG["CONTROL_FILE"])
            env = os.environ.copy()
            env["PUMP_BOT_MANAGED"] = "1"
            proc, tree = procctl.spawn_python(
                ROOT / "pump_scanner_bot.py", cwd=ROOT, env=env,
            )
            self._proc = proc
            self._tree = tree
            self._managed_mode = mode
            self._last_job_warning = tree.error
            atomic_write_json(process_file(mode), {
                "mode": mode,
                "status": "STARTING",
                "pid": proc.pid,
                "process_identity": procctl.process_identity(proc.pid),
                "managed": True,
                "started_at": time.time(),
                "updated_at": time.time(),
                "exit_code": None,
                "reason": None,
            })
            return self.status(mode)

    def _position(self, config: dict) -> dict:
        raw = state_mod.load_state(config["STATE_FILE"]) if Path(config["STATE_FILE"]).exists() else {}
        try:
            qty = float(raw.get("qty", 0) or 0)
        except (TypeError, ValueError):
            qty = 0.0
        return {
            "has_position": bool(raw.get("current_symbol") and qty > 0),
            "symbol": raw.get("current_symbol"),
            "qty": qty,
            "entry_price": float(raw.get("entry_price", 0) or 0),
        }

    def position(self, mode: str | None = None) -> dict:
        cfgmod = self._config()
        if mode is None or _mode(mode) == cfgmod.get_mode(cfgmod.PUMP_CONFIG):
            cfg = cfgmod.PUMP_CONFIG
        else:
            cfg, _ = cfgmod.build_config_for_mode(mode)
        return self._position(cfg)

    def _sell_first(self, cfg: dict, timeout: float) -> None:
        position = self._position(cfg)
        if not position["has_position"]:
            return
        state_mod.save_control(cfg["CONTROL_FILE"], {
            "action": "CLOSE_POSITION",
            "symbol": position["symbol"],
            "requested_at": state_mod.now_ms(),
        })
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._position(cfg)["has_position"]:
                return
            status = self.status(cfg["MODE"])
            if status["status"] not in ("STARTING", "RUNNING", "STOPPING"):
                raise BotControlError("Bot berhenti sebelum posisi berhasil dijual.")
            time.sleep(0.2)
        raise BotControlError("Posisi belum berhasil dijual sebelum timeout. Bot tetap dijalankan.")

    def stop(self, *, position_policy: str = "REQUIRE_EMPTY",
             graceful_timeout: float = 25.0, signal_timeout: float = 8.0) -> dict:
        cfgmod = self._config()
        with self._lock:
            self._intentional_stop = True
            self._auto_restart_mode = None
            mode = cfgmod.require_valid_mode(cfgmod.PUMP_CONFIG)
            cfg = cfgmod.PUMP_CONFIG
            status = self.status(mode)
            if status["status"] in ("STOPPED", "CRASHED"):
                return status
            policy = str(position_policy or "REQUIRE_EMPTY").strip().upper()
            position = self._position(cfg)
            if position["has_position"]:
                if policy == "SELL_FIRST":
                    self._sell_first(cfg, max(30.0, float(cfg.get("LOOP_INTERVAL_SECONDS", 15)) * 5))
                elif policy != "KEEP_OPEN":
                    raise BotControlError("Ada posisi terbuka. Pilih SELL_FIRST atau KEEP_OPEN.")

            pid = int(status["pid"])
            expected_identity = status.get("process_identity")
            managed_group = bool(self._proc is not None and self._managed_mode == mode and self._proc.pid == pid)
            state_mod.request_stop(cfg["CONTROL_FILE"])
            lifecycle = self._read_lifecycle(mode)
            lifecycle.update({"mode": mode, "status": "STOPPING", "pid": pid,
                              "updated_at": time.time(), "managed": managed_group})
            atomic_write_json(process_file(mode), lifecycle)

            if self._wait_dead(pid, graceful_timeout, expected_identity):
                self._finish_stop(mode)
                return self.status(mode)

            # Cek identitas tepat sebelum sinyal agar PID yang sudah didaur
            # ulang tidak pernah menerima sinyal milik bot lama.
            if expected_identity and not procctl.is_process_alive(pid, expected_identity):
                self._finish_stop(mode)
                return self.status(mode)
            try:
                procctl.send_graceful_signal(pid, process_group=managed_group)
            except (OSError, ProcessLookupError, RuntimeError):
                pass
            if self._wait_dead(pid, signal_timeout, expected_identity):
                self._finish_stop(mode)
                return self.status(mode)

            if expected_identity and not procctl.is_process_alive(pid, expected_identity):
                self._finish_stop(mode)
                return self.status(mode)
            try:
                procctl.force_kill(pid, self._tree if managed_group else None,
                                   process_group=managed_group)
            except (OSError, ProcessLookupError):
                pass
            self._wait_dead(pid, 5.0, expected_identity)
            self._finish_stop(mode, forced=True)
            return self.status(mode)

    def _wait_dead(self, pid: int, timeout: float,
                   expected_identity: str | None = None) -> bool:
        """Tunggu proses mati dan reap child milik dashboard lewat poll()."""
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.pid == pid:
                if self._proc.poll() is not None:
                    return True
            elif not procctl.is_process_alive(pid, expected_identity):
                return True
            time.sleep(0.1)
        if self._proc is not None and self._proc.pid == pid:
            return self._proc.poll() is not None
        return not procctl.is_process_alive(pid, expected_identity)

    def _finish_stop(self, mode: str, forced: bool = False) -> None:
        code = None
        if self._proc is not None and self._managed_mode == mode:
            code = self._proc.poll()
            if code is None and forced:
                code = 1
            self._record_managed_exit(mode, int(code or 0))
            self._clear_managed_handles()
        else:
            current = self._read_lifecycle(mode)
            current.update({
                "mode": mode,
                "status": "CRASHED" if forced else "STOPPED",
                "updated_at": time.time(),
                "exit_code": 1 if forced else current.get("exit_code", 0),
                "reason": "Dihentikan paksa setelah timeout." if forced else "Stop graceful dari dashboard.",
            })
            atomic_write_json(process_file(mode), current)

    def restart(self, *, position_policy: str = "REQUIRE_EMPTY") -> dict:
        self.stop(position_policy=position_policy)
        # Muat ulang override setelah proses lama benar-benar berhenti.
        cfgmod = self._config()
        cfgmod.reload_config()
        return self.start()

    def shutdown_dashboard(self) -> None:
        """Hentikan child yang dikelola saat dashboard keluar normal."""
        self._watchdog_stop.set()
        with self._lock:
            self._intentional_stop = True
            self._auto_restart_mode = None
            if self._proc is None or self._proc.poll() is not None:
                self._clear_managed_handles()
                return
        try:
            self.stop(position_policy="KEEP_OPEN", graceful_timeout=25, signal_timeout=8)
        except Exception:
            with self._lock:
                if self._proc is not None and self._proc.poll() is None:
                    try:
                        procctl.force_kill(self._proc.pid, self._tree, process_group=True)
                    except Exception:
                        pass
                self._clear_managed_handles()
