"""
Tes persistensi state PAPER (paper_store.py): restart di tengah posisi terbuka
dan penanganan file state korup.
"""

from __future__ import annotations

import glob
import json
import os
from decimal import Decimal

from trading.paper.paper_store import PaperStore, load_account_snapshot


def test_restart_persists_balances_and_open_orders(tmp_path):
    path = str(tmp_path / "acct.json")
    s1 = PaperStore(path, {"USDT": 5000.0})
    s1.debit("USDT", Decimal("1000"))
    s1.credit("PEPE", Decimal("100"))
    s1.add_open_order({"orderId": 1, "symbol": "PEPEUSDT", "status": "NEW",
                       "side": "BUY", "type": "LIMIT"})
    s1.add_trade({"orderId": 1, "symbol": "PEPEUSDT"})
    s1.save()

    s2 = PaperStore(path, {"USDT": 5000.0})
    assert s2.get_free("USDT") == Decimal("4000")
    assert s2.get_free("PEPE") == Decimal("100")
    assert len(s2.get_open_orders()) == 1
    assert s2.get_open_orders()[0]["orderId"] == 1
    assert len(s2.state["trade_history"]) == 1
    assert s2.state["schema_version"] == 1


def test_corrupt_state_backed_up_not_overwritten(tmp_path):
    path = str(tmp_path / "acct.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ ini bukan json valid ]]]")

    s = PaperStore(path, {"USDT": 777.0})
    backups = glob.glob(path + ".corrupt-*")
    assert backups, "harus ada file cadangan .corrupt-*"
    with open(backups[0], "r", encoding="utf-8") as f:
        assert "bukan json valid" in f.read()
    assert s.get_free("USDT") == Decimal("777")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["schema_version"] == 1


def test_atomic_save_leaves_no_tmp(tmp_path):
    path = str(tmp_path / "acct.json")
    s = PaperStore(path, {"USDT": 100.0})
    s.credit("USDT", Decimal("1"))
    s.save()
    assert not os.path.exists(path + ".tmp")
    assert os.path.exists(path)


def test_readonly_snapshot_does_not_write(tmp_path):
    missing = str(tmp_path / "nope.json")
    snap = load_account_snapshot(missing)
    assert snap["balances"] == []
    assert not os.path.exists(missing)


def test_migration_from_versionless(tmp_path):
    path = str(tmp_path / "acct.json")
    legacy = {"balances": {"USDT": {"free": "50", "locked": "0"}},
              "open_orders": [], "order_history": [], "trade_history": [],
              "total_fees": {}, "next_order_id": 1}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(legacy, f)
    s = PaperStore(path, {"USDT": 999.0})
    assert s.state["schema_version"] == 1
    assert s.get_free("USDT") == Decimal("50")
    assert "seen_client_order_ids" in s.state
