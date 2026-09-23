#!/usr/bin/env python3
"""
Peluncur gabungan: menjalankan Pump Scanner Bot + Dashboard web dalam SATU
perintah (`python run.py`), di satu terminal, dengan log tercampur.

Kenapa dijalankan sebagai 2 proses terpisah (bukan 2 thread dalam 1 proses)?
- `pump_scanner_bot.run()` memasang signal handler sendiri (SIGINT/SIGTERM)
  dan punya loop blocking dengan `time.sleep`. Flask `app.run()` juga
  blocking. Menjalankan keduanya sebagai proses terpisah menghindari
  konflik signal handler & membuat satu proses yang crash tidak mewariskan
  state rusak ke proses lain (GIL, exception tak tertangani, dsb).
- Output kedua proses TIDAK di-capture/di-buffer ulang -- keduanya mewarisi
  stdout/stderr terminal ini secara langsung, sehingga log benar-benar
  tercampur real-time seperti menjalankan `python bot.py & python dashboard.py`
  di satu layar, tanpa delay buffering tambahan dari peluncur ini.

PERILAKU (sesuai pilihan Anda):
- Kalau SALAH SATU proses berhenti/crash (apa pun sebabnya), proses yang
  satunya IKUT DIHENTIKAN dan peluncur ini keluar. Ini "semua atau tidak
  sama sekali" -- kalau bot berhenti, dashboard yang menampilkan data bot
  itu juga tidak ada gunanya tetap menyala sendirian.
- Ctrl+C (SIGINT) atau `kill` (SIGTERM) ke peluncur ini akan mematikan
  KEDUA proses secara berurutan (SIGTERM dulu, tunggu, baru SIGKILL kalau
  masih menolak berhenti) sebelum peluncur ini keluar.

CARA PAKAI:
    python run.py                       # bot (port bawaan config) + dashboard di :8080
    DASHBOARD_PORT=9000 python run.py   # ganti port dashboard

Menjalankan bot & dashboard terpisah (tanpa peluncur ini) MASIH tetap bisa:
    python pump_scanner_bot.py
    python dashboard.py
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from config import PUMP_CONFIG, get_mode, get_base_url

HERE = os.path.dirname(os.path.abspath(__file__))

_shutting_down = False


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} | RUN.PY  | {msg}", flush=True)


def _start(name: str, script: str) -> subprocess.Popen:
    """Mulai satu script Python sebagai proses anak, mewarisi stdout/stderr
    terminal ini apa adanya (supaya log tercampur tanpa buffering tambahan).
    `start_new_session=True` (POSIX) supaya proses anak punya process group
    sendiri, sehingga sinyal ke run.py tidak otomatis "bocor" ganda ke anak
    sebelum kita kirim sendiri secara terkendali."""
    kwargs = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [sys.executable, script],
        cwd=HERE,
        stdout=None,
        stderr=None,
        **kwargs,
    )
    _log(f"{name} dimulai (PID {proc.pid}).")
    return proc


def _terminate(name: str, proc: "subprocess.Popen | None", timeout: float = 10.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    _log(f"Menghentikan {name} (PID {proc.pid})...")
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    except OSError:
        pass

    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            _log(f"{name} berhenti (kode keluar {proc.returncode}).")
            return
        time.sleep(0.2)

    _log(f"{name} tidak berhenti dalam {timeout:.0f} detik, dipaksa berhenti (SIGKILL).")
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, OSError):
        pass


def _handle_signal(signum, frame):
    global _shutting_down
    _shutting_down = True
    _log(f"Menerima sinyal berhenti ({signal.Signals(signum).name}). Mematikan bot & dashboard...")


def main() -> int:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    port = os.environ.get("DASHBOARD_PORT", "8080")
    mode = get_mode(PUMP_CONFIG)
    base_url = get_base_url(PUMP_CONFIG)
    risk_pct = PUMP_CONFIG.get("RISK_PERCENT")

    _log("=" * 70)
    _log("Menjalankan Pump Scanner Bot + Dashboard dalam satu perintah.")
    mode_label = ("TESTNET (order sungguhan, dana virtual)" if mode == "TESTNET"
                  else "LIVE (order sungguhan, UANG ASLI!)")
    _log(f"Mode bot: {mode_label} | endpoint={base_url} | RISK_PERCENT={risk_pct}%")
    # Ikuti pengaturan yang sama dengan dashboard.py. Bawaannya hanya
    # komputer ini, karena dashboard tidak punya login sementara ada
    # tombol yang bisa menjual posisi sungguhan.
    dash_host = os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
    dash_label = "localhost" if dash_host == "127.0.0.1" else dash_host
    _log(f"Dashboard akan tersedia di http://{dash_label}:{port}")
    if dash_host == "0.0.0.0":
        _log("PERINGATAN: dashboard terbuka ke seluruh jaringan tanpa password.")
    _log("Tekan Ctrl+C untuk menghentikan KEDUANYA sekaligus.")
    _log("=" * 70)

    if not PUMP_CONFIG.get("API_KEY") or not PUMP_CONFIG.get("API_SECRET"):
        _log("BERHENTI: BINANCE_API_KEY/BINANCE_API_SECRET belum di-set di file .env. "
             f"Mode {mode} tetap mengirim order sungguhan, jadi kredensial wajib ada. "
             "Untuk mode TESTNET, buat key gratis di https://testnet.binance.vision "
             "(key produksi TIDAK berlaku di testnet, dan sebaliknya).")
        return 1

    bot_proc = _start("Pump Scanner Bot", "pump_scanner_bot.py")
    dash_proc = _start("Dashboard", "dashboard.py")

    exit_code = 0
    try:
        while not _shutting_down:
            bot_rc = bot_proc.poll()
            dash_rc = dash_proc.poll()

            if bot_rc is not None:
                _log(f"Pump Scanner Bot berhenti sendiri (kode keluar {bot_rc}). "
                     "Dashboard ikut dihentikan (tidak ada gunanya jalan tanpa bot).")
                exit_code = bot_rc if bot_rc else 1
                break

            if dash_rc is not None:
                _log(f"Dashboard berhenti sendiri (kode keluar {dash_rc}). "
                     "Bot ikut dihentikan sesuai kebijakan \"semua atau tidak sama sekali\".")
                exit_code = dash_rc if dash_rc else 1
                break

            time.sleep(1.0)
    finally:
        _terminate("Dashboard", dash_proc)
        _terminate("Pump Scanner Bot", bot_proc)

    if _shutting_down:
        _log("Semua proses berhenti dengan bersih (diminta pengguna).")
        return 0

    _log(f"Semua proses berhenti (salah satu berhenti tak terduga). Kode keluar akhir: {exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
