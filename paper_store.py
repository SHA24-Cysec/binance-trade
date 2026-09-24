"""
Persistensi state akun simulasi PAPER.

Menyimpan saldo virtual per aset, order terbuka, riwayat order, riwayat trade,
total fee, dan versi skema ke satu file JSON. Dipisah dari file state posisi bot
(pump_bot_state_*.json) dan TIDAK pernah dipakai di LIVE, sehingga data PAPER
dan LIVE tidak mungkin tercampur.

Kenapa JSON, bukan SQLite?
- Konsisten dengan state.py yang sudah memakai JSON atomik.
- Volume tulis rendah (satu posisi aktif per rotasi, order jarang).
- Mudah diperiksa/di-reset manusia (cukup hapus/pindahkan file).
- Tanpa dependensi tambahan.
Trade-off: query historis besar kurang efisien -- dapat diterima karena
riwayat kecil. Bila kelak butuh analitik berat, migrasi ke SQLite mudah karena
akses state sudah terbungkus di kelas ini.

Ketahanan:
- Tulis atomik: tulis ke file .tmp lalu os.replace (atomik di POSIX & Windows),
  jadi file tidak pernah setengah-tertulis walau proses mati mendadak.
- Semua akses dijaga threading.RLock (loop bot + thread WS + dashboard).
- File korup saat load: dibuat cadangan *.corrupt-<ts> lalu diberi tahu lewat
  log, TIDAK ditimpa diam-diam. State direset ke saldo awal.
- schema_version + migrasi sederhana (registry fungsi per versi).

Semua nilai uang/qty disimpan sebagai STRING desimal (mempertahankan presisi)
dan dipakai sebagai Decimal di memori.

Versi acuan: Python 3.10+ (Decimal, os.replace).
"""

from __future__ import annotations

import json
import logging
import os
import time
from decimal import Decimal
from typing import Any, Callable, Optional
import threading

logger = logging.getLogger("paper_store")

SCHEMA_VERSION = 1

_ZERO = Decimal("0")


def _d(value: Any) -> Decimal:
    """Konversi aman ke Decimal (lewat str agar tidak menyerap galat float)."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def load_account_snapshot(path: str) -> dict:
    """Baca file state PAPER secara READ-ONLY dan kembalikan bentuk seperti
    GET /api/v3/account, TANPA membuat/menulis file apa pun.

    Dipakai proses DASHBOARD (terpisah dari bot) agar bisa menampilkan saldo
    virtual tanpa risiko balapan tulis dengan proses bot yang memegang file
    yang sama. Bila file belum ada / rusak, kembalikan akun kosong.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        balances = []
        for asset, b in sorted((data.get("balances") or {}).items()):
            balances.append({
                "asset": asset,
                "free": str(_d(b.get("free", 0))),
                "locked": str(_d(b.get("locked", 0))),
            })
        return {
            "accountType": "SPOT",
            "balances": balances,
            "canTrade": True,
            "canWithdraw": False,
            "canDeposit": False,
            "permissions": ["SPOT"],
            "updateTime": int(data.get("created_at_ms", 0)),
            "totalFees": data.get("total_fees", {}),
        }
    except (json.JSONDecodeError, OSError, ValueError, TypeError, AttributeError):
        return {"accountType": "SPOT", "balances": [], "permissions": ["SPOT"]}


class PaperStore:
    """State akun simulasi PAPER yang persisten dan aman-thread."""

    def __init__(self, path: str, initial_balances: Optional[dict] = None) -> None:
        self.path = path
        self.initial_balances = dict(initial_balances or {"USDT": 10000.0})
        self.lock = threading.RLock()
        self.state: dict[str, Any] = {}
        self._load_or_init()

    # ------------------------------------------------------------------
    # Muat / inisialisasi
    # ------------------------------------------------------------------
    def _default_state(self) -> dict[str, Any]:
        balances = {}
        for asset, amount in self.initial_balances.items():
            balances[str(asset).upper()] = {"free": str(_d(amount)), "locked": "0"}
        return {
            "schema_version": SCHEMA_VERSION,
            "balances": balances,
            "open_orders": [],
            "order_history": [],
            "trade_history": [],
            "total_fees": {},
            "next_order_id": 1,
            "seen_client_order_ids": [],
            "created_at_ms": int(time.time() * 1000),
        }

    def _load_or_init(self) -> None:
        with self.lock:
            if not os.path.exists(self.path):
                logger.info("File state PAPER %s belum ada. Memulai saldo awal: %s",
                            self.path, self.initial_balances)
                self.state = self._default_state()
                self._save_locked()
                return
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("root state bukan objek JSON")
                data = self._migrate(data)
                self._validate(data)
                self.state = data
                logger.info("State PAPER dimuat dari %s (versi skema %s).",
                            self.path, data.get("schema_version"))
            except (json.JSONDecodeError, OSError, ValueError, KeyError, TypeError) as exc:
                self._backup_corrupt(exc)
                self.state = self._default_state()
                self._save_locked()

    def _backup_corrupt(self, exc: Exception) -> None:
        backup = f"{self.path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            os.replace(self.path, backup)
            logger.error(
                "File state PAPER %s RUSAK (%s). Cadangan disimpan ke %s. "
                "State direset ke saldo awal -- file lama TIDAK ditimpa diam-diam.",
                self.path, exc, backup,
            )
        except OSError as move_exc:
            logger.error(
                "File state PAPER %s rusak (%s) dan cadangan gagal dibuat (%s). "
                "Melanjutkan dengan state saldo awal di memori.",
                self.path, exc, move_exc,
            )

    def _validate(self, data: dict) -> None:
        for key in ("balances", "open_orders", "order_history", "trade_history",
                    "total_fees", "next_order_id"):
            if key not in data:
                raise KeyError(f"kunci wajib '{key}' hilang di state")
        if not isinstance(data["balances"], dict):
            raise ValueError("balances harus objek")
        # Validasi nilai saldo bisa diparse jadi Decimal.
        for asset, bal in data["balances"].items():
            _d(bal.get("free", 0))
            _d(bal.get("locked", 0))

    # ------------------------------------------------------------------
    # Migrasi skema
    # ------------------------------------------------------------------
    def _migrate(self, data: dict) -> dict:
        version = int(data.get("schema_version", 0))
        # Registry migrasi: fungsi yang menaikkan versi N -> N+1.
        migrations: dict[int, Callable[[dict], dict]] = {
            0: self._migrate_0_to_1,
        }
        while version < SCHEMA_VERSION:
            fn = migrations.get(version)
            if fn is None:
                raise ValueError(
                    f"Tidak ada jalur migrasi dari versi skema {version} ke {SCHEMA_VERSION}")
            logger.info("Migrasi state PAPER versi %d -> %d", version, version + 1)
            data = fn(data)
            version = int(data.get("schema_version", version + 1))
        return data

    def _migrate_0_to_1(self, data: dict) -> dict:
        """Versi 0 (tanpa nomor) -> 1: lengkapi kunci yang hilang dengan default.
        Contoh migrasi sederhana; tambah handler baru bila skema berkembang."""
        base = self._default_state()
        base.update({k: v for k, v in data.items() if k != "schema_version"})
        base["schema_version"] = 1
        return base

    # ------------------------------------------------------------------
    # Penyimpanan atomik
    # ------------------------------------------------------------------
    def _save_locked(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)  # atomik

    def save(self) -> None:
        with self.lock:
            self._save_locked()

    # ------------------------------------------------------------------
    # Operasi saldo (semua di bawah lock)
    # ------------------------------------------------------------------
    def _bal(self, asset: str) -> dict:
        a = asset.upper()
        b = self.state["balances"].get(a)
        if b is None:
            b = {"free": "0", "locked": "0"}
            self.state["balances"][a] = b
        return b

    def get_free(self, asset: str) -> Decimal:
        with self.lock:
            return _d(self._bal(asset)["free"])

    def get_locked(self, asset: str) -> Decimal:
        with self.lock:
            return _d(self._bal(asset)["locked"])

    def credit(self, asset: str, amount: Decimal) -> None:
        """Tambah saldo free."""
        with self.lock:
            b = self._bal(asset)
            b["free"] = str(_d(b["free"]) + _d(amount))

    def debit(self, asset: str, amount: Decimal) -> None:
        """Kurangi saldo free. Melempar bila tidak cukup (proteksi konsistensi)."""
        with self.lock:
            b = self._bal(asset)
            free = _d(b["free"])
            amt = _d(amount)
            if amt > free:
                raise ValueError(f"Saldo {asset} tidak cukup: butuh {amt}, tersedia {free}")
            b["free"] = str(free - amt)

    def lock_funds(self, asset: str, amount: Decimal) -> None:
        with self.lock:
            b = self._bal(asset)
            free = _d(b["free"]); amt = _d(amount)
            if amt > free:
                raise ValueError(f"Saldo {asset} tidak cukup untuk dikunci: {amt} > {free}")
            b["free"] = str(free - amt)
            b["locked"] = str(_d(b["locked"]) + amt)

    def unlock_funds(self, asset: str, amount: Decimal) -> None:
        with self.lock:
            b = self._bal(asset)
            locked = _d(b["locked"]); amt = _d(amount)
            take = amt if amt <= locked else locked
            b["locked"] = str(locked - take)
            b["free"] = str(_d(b["free"]) + take)

    def consume_locked(self, asset: str, amount: Decimal) -> None:
        """Ambil dari saldo locked (mis. saat limit order terisi)."""
        with self.lock:
            b = self._bal(asset)
            locked = _d(b["locked"]); amt = _d(amount)
            if amt > locked:
                raise ValueError(f"Locked {asset} tidak cukup: {amt} > {locked}")
            b["locked"] = str(locked - amt)

    def add_fee(self, asset: str, amount: Decimal) -> None:
        with self.lock:
            cur = _d(self.state["total_fees"].get(asset.upper(), 0))
            self.state["total_fees"][asset.upper()] = str(cur + _d(amount))

    # ------------------------------------------------------------------
    # Order & trade
    # ------------------------------------------------------------------
    def next_order_id(self) -> int:
        with self.lock:
            oid = int(self.state["next_order_id"])
            self.state["next_order_id"] = oid + 1
            return oid

    def is_duplicate_client_order_id(self, coid: Optional[str]) -> bool:
        if not coid:
            return False
        with self.lock:
            return coid in self.state["seen_client_order_ids"]

    def remember_client_order_id(self, coid: Optional[str]) -> None:
        if not coid:
            return
        with self.lock:
            seen = self.state["seen_client_order_ids"]
            seen.append(coid)
            # Batasi pertumbuhan tak terbatas: simpan 5000 terakhir.
            if len(seen) > 5000:
                del seen[: len(seen) - 5000]

    def add_open_order(self, order: dict) -> None:
        with self.lock:
            self.state["open_orders"].append(order)

    def update_open_order(self, order: dict) -> None:
        with self.lock:
            for i, o in enumerate(self.state["open_orders"]):
                if o.get("orderId") == order.get("orderId"):
                    self.state["open_orders"][i] = order
                    return
            self.state["open_orders"].append(order)

    def remove_open_order(self, order_id: int) -> Optional[dict]:
        with self.lock:
            for i, o in enumerate(self.state["open_orders"]):
                if o.get("orderId") == order_id:
                    return self.state["open_orders"].pop(i)
            return None

    def get_open_orders(self, symbol: Optional[str] = None) -> list:
        with self.lock:
            orders = list(self.state["open_orders"])
        if symbol:
            s = symbol.upper()
            return [o for o in orders if o.get("symbol") == s]
        return orders

    def find_order(self, order_id: Optional[int] = None,
                   orig_client_order_id: Optional[str] = None) -> Optional[dict]:
        with self.lock:
            pools = self.state["open_orders"] + self.state["order_history"]
            for o in pools:
                if order_id is not None and o.get("orderId") == order_id:
                    return o
                if orig_client_order_id is not None and o.get("clientOrderId") == orig_client_order_id:
                    return o
        return None

    def archive_order(self, order: dict) -> None:
        with self.lock:
            self.state["order_history"].append(order)
            if len(self.state["order_history"]) > 10000:
                del self.state["order_history"][: len(self.state["order_history"]) - 10000]

    def add_trade(self, trade: dict) -> None:
        with self.lock:
            self.state["trade_history"].append(trade)
            if len(self.state["trade_history"]) > 20000:
                del self.state["trade_history"][: len(self.state["trade_history"]) - 20000]

    # ------------------------------------------------------------------
    # Bentuk respons akun (seperti GET /api/v3/account)
    # ------------------------------------------------------------------
    def account_snapshot(self) -> dict:
        with self.lock:
            balances = []
            for asset, b in sorted(self.state["balances"].items()):
                balances.append({
                    "asset": asset,
                    "free": str(_d(b.get("free", 0))),
                    "locked": str(_d(b.get("locked", 0))),
                })
            return {
                "makerCommission": 0,
                "takerCommission": 0,
                "canTrade": True,
                "canWithdraw": False,
                "canDeposit": False,
                "accountType": "SPOT",
                "balances": balances,
                "permissions": ["SPOT"],
                "updateTime": int(time.time() * 1000),
            }


# ==== RINGKASAN AUDIT (paper_store.py) =================================
# Sintaks/tipe: type hints lengkap; Decimal via _d() (selalu lewat str).
# Atomik: _save_locked menulis .tmp + fsync + os.replace -> tidak pernah
#   setengah-tertulis. save() memegang lock.
# Korupsi: load rusak -> backup *.corrupt-<ts> via os.replace lalu reset ke
#   saldo awal; TIDAK menimpa diam-diam. Diuji di tests/test_store.py.
# Migrasi: registry per-versi; _migrate menaikkan bertahap sampai SCHEMA_VERSION;
#   jalur hilang -> error jelas (bukan data korup diam-diam).
# Race condition: SATU RLock membungkus semua baca/tulis state; operasi majemuk
#   di PaperClient membungkus beberapa panggilan dalam `with store.lock`.
# Konsistensi saldo: debit/consume_locked melempar bila tidak cukup -> bug di
#   hulu ketahuan, bukan saldo negatif diam-diam.
# Kebocoran rahasia: tidak menyimpan API key/secret sama sekali.
# =======================================================================
