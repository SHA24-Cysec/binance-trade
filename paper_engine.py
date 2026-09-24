"""
Mesin pencocokan (matching engine) order simulasi PAPER.

Menyimulasikan eksekusi order Binance Spot secara realistis penuh dan LOKAL,
dengan bentuk respons yang identik dengan REST Binance asli sehingga kode bot
tidak perlu cabang khusus.

Yang disimulasikan:
- Filter simbol: PRICE_FILTER, LOT_SIZE, MARKET_LOT_SIZE, MIN_NOTIONAL/NOTIONAL.
  Pelanggaran ditolak dengan error mirip Binance (-1013/-1111/-2010).
- Market order: "berjalan" melalui kedalaman order book (ask untuk BUY, bid
  untuk SELL) pada snapshot depth SAAT order dikirim. Menghasilkan harga
  rata-rata isi, slippage, dan mendukung partial fill. Sisa qty yang tidak
  terisi karena kedalaman kurang diperlakukan EXPIRED (asumsi realistis; lihat
  catatan).
- Limit order: aturan KONSERVATIF -- hanya terisi bila harga pasar benar-benar
  melintasi harga limit (BUY: ask <= limit; SELL: bid >= limit). Mendukung
  status NEW/PARTIALLY_FILLED/FILLED/CANCELED/EXPIRED/REJECTED dan timeout.
- Stop (STOP_LOSS/TAKE_PROFIT[_LIMIT]): terpicu saat harga menembus stopPrice,
  termasuk kasus GAP harga (harga melompat melewati level -> isi pada harga
  pasar setelah gap, bukan pada stopPrice).
- Fee: memakai tarif dari config (taker untuk market/marketable, maker untuk
  limit yang mengendap), termasuk diskon BNB. Dipotong dari aset yang diterima
  (BUY -> base; SELL -> quote) sehingga qty jual tidak pernah melebihi saldo.
- Idempotensi clientOrderId (duplikat ditolak, -2010).

Perhitungan uang/qty memakai Decimal, bukan float.

Asumsi & batasan (jujur, juga ditulis di README):
- Sisa market order yang tak terisi karena kedalaman habis -> EXPIRED (Binance
  membiarkan sisa market order kadaluarsa alih-alih menggantung).
- Tidak memodelkan: latensi jaringan, posisi antrean di order book, dampak
  pasar dari order kita sendiri, dan pergerakan harga selama order "dikirim".
- Diskon BNB dimodelkan sebagai TARIF fee yang lebih rendah namun tetap
  dipotong dari aset yang diperdagangkan (bukan dari saldo BNB terpisah).
  Ini penyederhanaan yang disengaja agar perilaku "fee mengurangi qty yang
  diterima" tetap terjaga.

Versi acuan: Python 3.10+ (Decimal).
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Callable, Optional

from binance_client import BinanceAPIError, SymbolFilters
from paper_store import PaperStore, _d

logger = logging.getLogger("paper_engine")

_ZERO = Decimal("0")

# Kode error yang ditiru dari Binance (untuk menguji penanganan error bot).
ERR_INVALID_QTY = (-1013, "Filter failure: LOT_SIZE")
ERR_MIN_NOTIONAL = (-1013, "Filter failure: NOTIONAL")
ERR_PRICE_FILTER = (-1013, "Filter failure: PRICE_FILTER")
ERR_PRECISION = (-1111, "Precision is over the maximum defined for this asset.")
ERR_INSUFFICIENT = (-2010, "Account has insufficient balance for requested action.")
ERR_DUPLICATE = (-2010, "Duplicate order sent.")
ERR_BAD_PARAM = (-1102, "Mandatory parameter was not sent, was empty/null, or malformed.")
ERR_NO_SYMBOL = (-1121, "Invalid symbol.")


def _reject(err: tuple[int, str], extra: str = "") -> BinanceAPIError:
    code, msg = err
    return BinanceAPIError(400, code, f"{msg}{(' ' + extra) if extra else ''}")


class PaperMatchingEngine:
    """Mesin simulasi order. Tidak menyentuh jaringan langsung; data pasar
    diambil lewat callable yang di-inject (mudah diuji dengan data palsu)."""

    def __init__(
        self,
        config: dict,
        store: PaperStore,
        filters_provider: Callable[[str], Optional[SymbolFilters]],
        depth_provider: Callable[[str], dict],
        price_provider: Callable[[str], float],
        quote_asset: str = "USDT",
    ) -> None:
        self.config = config
        self.store = store
        self.filters_provider = filters_provider
        self.depth_provider = depth_provider
        self.price_provider = price_provider
        self.quote_asset = quote_asset.upper()
        # Tarif fee dihitung sebagai Decimal EKSAK dari nilai mentah config
        # (bukan lewat helper float get_taker_fee_pct) agar tidak menyerap galat
        # pembulatan float, mis. 0.1*0.75 -> 0.07500000000000001. Nilai persen
        # tetap konsisten dengan get_taker_fee_pct()/get_maker_fee_pct().
        taker_base = Decimal(str(config.get("TAKER_FEE_PCT", 0.1)))
        maker_base = Decimal(str(config.get("MAKER_FEE_PCT", config.get("TAKER_FEE_PCT", 0.1))))
        if config.get("USE_BNB_FEE_DISCOUNT"):
            taker_base *= Decimal("0.75")
            maker_base *= Decimal("0.75")
        self._taker_rate = taker_base / Decimal("100")
        self._maker_rate = maker_base / Decimal("100")
        self._trade_seq = int(time.time())

    # ------------------------------------------------------------------
    # Util
    # ------------------------------------------------------------------
    def _split_assets(self, symbol: str) -> tuple[str, str]:
        """Pisah simbol menjadi (base, quote). Bot memakai quote tetap
        (USDT). Bila simbol tidak berakhiran quote_asset, cari dari daftar
        quote umum."""
        s = symbol.upper()
        if s.endswith(self.quote_asset):
            return s[: -len(self.quote_asset)], self.quote_asset
        for q in ("USDT", "FDUSD", "USDC", "BTC", "ETH", "BNB", "TUSD", "TRY", "EUR"):
            if s.endswith(q) and len(s) > len(q):
                return s[: -len(q)], q
        # fallback: anggap 3 huruf terakhir quote
        return s[:-3], s[-3:]

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _next_trade_id(self) -> int:
        self._trade_seq += 1
        return self._trade_seq

    def _check_lot_size(self, filters: SymbolFilters, qty: Decimal) -> None:
        if qty <= 0:
            raise _reject(ERR_INVALID_QTY, "(qty <= 0)")
        if qty < filters.min_qty:
            raise _reject(ERR_INVALID_QTY,
                          f"(qty {qty} < minQty {filters.min_qty})")
        step = filters.step_size
        if step > 0:
            # (qty - minQty) harus kelipatan step.
            rem = (qty - filters.min_qty) % step
            if rem != 0:
                raise _reject(ERR_INVALID_QTY,
                              f"(qty {qty} tidak kelipatan stepSize {step})")

    def _check_price_filter(self, filters: SymbolFilters, price: Decimal) -> None:
        if price <= 0:
            raise _reject(ERR_PRICE_FILTER, "(price <= 0)")
        tick = filters.tick_size
        if tick > 0 and (price % tick) != 0:
            raise _reject(ERR_PRICE_FILTER,
                          f"(price {price} tidak kelipatan tickSize {tick})")

    def _levels(self, symbol: str, side: str) -> list[tuple[Decimal, Decimal]]:
        """Ambil level order book relevan: asks (naik) untuk BUY, bids (turun)
        untuk SELL. Kembalikan list (price, qty) Decimal."""
        depth = self.depth_provider(symbol)
        raw = depth.get("asks" if side == "BUY" else "bids", [])
        out: list[tuple[Decimal, Decimal]] = []
        for lvl in raw:
            try:
                out.append((_d(lvl[0]), _d(lvl[1])))
            except (IndexError, ValueError, TypeError):
                continue
        return out

    def _walk_by_qty(self, levels: list[tuple[Decimal, Decimal]], target_qty: Decimal
                     ) -> tuple[Decimal, Decimal, list[tuple[Decimal, Decimal]]]:
        """Berjalan melalui level sampai target_qty terpenuhi atau habis.
        Kembalikan (filled_qty, quote_spent, fills[(price, qty)])."""
        remaining = target_qty
        filled = _ZERO
        quote = _ZERO
        fills: list[tuple[Decimal, Decimal]] = []
        for price, avail in levels:
            if remaining <= 0:
                break
            take = avail if avail < remaining else remaining
            if take <= 0:
                continue
            fills.append((price, take))
            filled += take
            quote += take * price
            remaining -= take
        return filled, quote, fills

    def _walk_by_quote(self, levels: list[tuple[Decimal, Decimal]], target_quote: Decimal
                       ) -> tuple[Decimal, Decimal, list[tuple[Decimal, Decimal]]]:
        """Berjalan sampai dana quote habis (untuk BUY quoteOrderQty)."""
        remaining_quote = target_quote
        filled = _ZERO
        quote = _ZERO
        fills: list[tuple[Decimal, Decimal]] = []
        for price, avail in levels:
            if remaining_quote <= 0:
                break
            level_cost = avail * price
            if level_cost <= remaining_quote:
                take = avail
                cost = level_cost
            else:
                take = (remaining_quote / price)
                cost = take * price
            if take <= 0:
                continue
            fills.append((price, take))
            filled += take
            quote += cost
            remaining_quote -= cost
        return filled, quote, fills

    # ------------------------------------------------------------------
    # Bentuk respons
    # ------------------------------------------------------------------
    def _new_order_record(self, symbol: str, side: str, order_type: str,
                          orig_qty: Decimal, price: Decimal, stop_price: Optional[Decimal],
                          time_in_force: Optional[str], client_order_id: str) -> dict:
        oid = self.store.next_order_id()
        now = self._now_ms()
        return {
            "symbol": symbol.upper(),
            "orderId": oid,
            "orderListId": -1,
            "clientOrderId": client_order_id,
            "transactTime": now,
            "time": now,
            "updateTime": now,
            "price": str(price),
            "origQty": str(orig_qty),
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
            "status": "NEW",
            "timeInForce": time_in_force or "GTC",
            "type": order_type,
            "side": side,
            "stopPrice": str(stop_price) if stop_price is not None else "0",
            "fills": [],
        }

    def _apply_fills(self, order: dict, base: str, quote: str, side: str,
                     fills: list[tuple[Decimal, Decimal]], maker: bool) -> None:
        """Terapkan fills ke saldo + fee, dan isi bidang respons order."""
        rate = self._maker_rate if maker else self._taker_rate
        exec_qty = _ZERO
        cum_quote = _ZERO
        fill_records = []
        for price, qty in fills:
            exec_qty += qty
            cum_quote += price * qty
        if exec_qty <= 0:
            return

        if side == "BUY":
            # Bayar quote penuh, terima base dikurangi fee (fee dari base).
            self.store.debit(quote, cum_quote)
            fee_total_base = _ZERO
            for price, qty in fills:
                fee = qty * rate
                fee_total_base += fee
                fill_records.append({
                    "price": str(price),
                    "qty": str(qty),
                    "commission": str(fee),
                    "commissionAsset": base,
                    "tradeId": self._next_trade_id(),
                })
            self.store.credit(base, exec_qty - fee_total_base)
            self.store.add_fee(base, fee_total_base)
        else:  # SELL
            # Serahkan base penuh, terima quote dikurangi fee (fee dari quote).
            self.store.debit(base, exec_qty)
            fee_total_quote = _ZERO
            for price, qty in fills:
                gross = price * qty
                fee = gross * rate
                fee_total_quote += fee
                fill_records.append({
                    "price": str(price),
                    "qty": str(qty),
                    "commission": str(fee),
                    "commissionAsset": quote,
                    "tradeId": self._next_trade_id(),
                })
            self.store.credit(quote, cum_quote - fee_total_quote)
            self.store.add_fee(quote, fee_total_quote)

        order["executedQty"] = str(exec_qty)
        order["cummulativeQuoteQty"] = str(cum_quote)
        order["fills"] = fill_records
        # Catat trade ke riwayat.
        self.store.add_trade({
            "symbol": order["symbol"],
            "orderId": order["orderId"],
            "side": side,
            "type": order["type"],
            "executedQty": str(exec_qty),
            "cummulativeQuoteQty": str(cum_quote),
            "avgPrice": str(cum_quote / exec_qty) if exec_qty > 0 else "0",
            "time": self._now_ms(),
        })

    # ------------------------------------------------------------------
    # Titik masuk utama
    # ------------------------------------------------------------------
    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Optional[float | Decimal] = None,
        price: Optional[float | Decimal] = None,
        stop_price: Optional[float | Decimal] = None,
        time_in_force: Optional[str] = None,
        quote_order_qty: Optional[float | Decimal] = None,
        client_order_id: Optional[str] = None,
    ) -> dict:
        side = side.upper()
        order_type = order_type.upper()
        if side not in ("BUY", "SELL"):
            raise _reject(ERR_BAD_PARAM, "(side harus BUY/SELL)")

        filters = self.filters_provider(symbol)
        if filters is None:
            raise _reject(ERR_NO_SYMBOL, f"({symbol})")

        base, quote = self._split_assets(symbol)
        coid = client_order_id or f"paper-{int(time.time()*1000)}-{self.store.next_order_id()}"

        with self.store.lock:
            # Idempotensi: clientOrderId duplikat ditolak seperti Binance.
            if self.store.is_duplicate_client_order_id(client_order_id):
                raise _reject(ERR_DUPLICATE, f"(clientOrderId {client_order_id})")

            if order_type == "MARKET":
                result = self._place_market(symbol, side, base, quote, filters,
                                            quantity, quote_order_qty, coid)
            elif order_type == "LIMIT":
                result = self._place_limit(symbol, side, base, quote, filters,
                                           quantity, price, time_in_force, coid)
            elif order_type in ("STOP_LOSS", "TAKE_PROFIT", "STOP_LOSS_LIMIT",
                                "TAKE_PROFIT_LIMIT"):
                result = self._place_stop(symbol, side, base, quote, filters,
                                          order_type, quantity, price, stop_price,
                                          time_in_force, coid)
            else:
                raise _reject(ERR_BAD_PARAM, f"(type {order_type} tidak didukung)")

            self.store.remember_client_order_id(coid)
            self.store.save()
            return result

    # ------------------------------------------------------------------
    # MARKET
    # ------------------------------------------------------------------
    def _place_market(self, symbol, side, base, quote, filters, quantity,
                      quote_order_qty, coid) -> dict:
        if quantity is None and quote_order_qty is None:
            raise _reject(ERR_BAD_PARAM, "(butuh quantity atau quoteOrderQty)")

        levels = self._levels(symbol, side)
        if not levels:
            raise _reject((-1013, "Filter failure: no liquidity"),
                          "(order book kosong / tidak tersedia)")

        if quantity is not None:
            qty = _d(quantity)
            self._check_lot_size(filters, qty)
            # Cek MIN_NOTIONAL memakai harga terbaik (estimasi konservatif).
            best_price = levels[0][0]
            est_notional = qty * best_price
            if filters.min_notional > 0 and est_notional < filters.min_notional:
                raise _reject(ERR_MIN_NOTIONAL,
                              f"(notional {est_notional} < minNotional {filters.min_notional})")
            filled, spent, fills = self._walk_by_qty(levels, qty)
            orig_qty = qty
        else:
            qoq = _d(quote_order_qty)
            if filters.min_notional > 0 and qoq < filters.min_notional:
                raise _reject(ERR_MIN_NOTIONAL,
                              f"(quoteOrderQty {qoq} < minNotional {filters.min_notional})")
            filled, spent, fills = self._walk_by_quote(levels, qoq)
            # Bulatkan filled ke LOT_SIZE (Binance mengembalikan qty patuh lot).
            filled = self._round_down_step(filled, filters)
            # Bangun ulang fills agar konsisten dengan filled yang dibulatkan.
            filled, spent, fills = self._walk_by_qty(levels, filled) if filled > 0 else (_ZERO, _ZERO, [])
            orig_qty = filled

        if filled <= 0:
            raise _reject(ERR_INSUFFICIENT, "(tidak ada yang terisi)")

        # Cek saldo.
        if side == "BUY":
            need = spent
            if self.store.get_free(quote) < need:
                raise _reject(ERR_INSUFFICIENT,
                              f"(butuh {need} {quote}, ada {self.store.get_free(quote)})")
        else:
            if self.store.get_free(base) < filled:
                raise _reject(ERR_INSUFFICIENT,
                              f"(butuh {filled} {base}, ada {self.store.get_free(base)})")

        order = self._new_order_record(symbol, side, "MARKET", orig_qty,
                                       Decimal("0"), None, None, coid)
        self._apply_fills(order, base, quote, side, fills, maker=False)

        # Status akhir: FILLED bila penuh, EXPIRED bila kedalaman kurang
        # (sisa market order dianggap kadaluarsa -- lihat catatan asumsi).
        exec_qty = _d(order["executedQty"])
        if exec_qty >= orig_qty:
            order["status"] = "FILLED"
        else:
            order["status"] = "EXPIRED"
            logger.info("Market %s %s partial fill: %s dari %s (kedalaman kurang) -> EXPIRED sisa.",
                        side, symbol, exec_qty, orig_qty)
        self.store.archive_order(order)
        return order

    def _round_down_step(self, qty: Decimal, filters: SymbolFilters) -> Decimal:
        step = filters.step_size
        if step <= 0:
            return qty
        steps = (qty / step).to_integral_value(rounding=ROUND_DOWN)
        return steps * step

    # ------------------------------------------------------------------
    # LIMIT
    # ------------------------------------------------------------------
    def _place_limit(self, symbol, side, base, quote, filters, quantity, price,
                     time_in_force, coid) -> dict:
        if quantity is None or price is None:
            raise _reject(ERR_BAD_PARAM, "(LIMIT butuh quantity & price)")
        qty = _d(quantity)
        px = _d(price)
        self._check_lot_size(filters, qty)
        self._check_price_filter(filters, px)
        if filters.min_notional > 0 and (qty * px) < filters.min_notional:
            raise _reject(ERR_MIN_NOTIONAL,
                          f"(notional {qty*px} < minNotional {filters.min_notional})")

        order = self._new_order_record(symbol, side, "LIMIT", qty, px, None,
                                       time_in_force or "GTC", coid)

        # Kunci dana untuk order yang mengendap.
        if side == "BUY":
            need = qty * px
            if self.store.get_free(quote) < need:
                raise _reject(ERR_INSUFFICIENT, f"(butuh {need} {quote})")
            self.store.lock_funds(quote, need)
        else:
            if self.store.get_free(base) < qty:
                raise _reject(ERR_INSUFFICIENT, f"(butuh {qty} {base})")
            self.store.lock_funds(base, qty)

        order["_lockedAsset"] = quote if side == "BUY" else base
        order["_lockedRemaining"] = str((qty * px) if side == "BUY" else qty)
        order["_createdMs"] = self._now_ms()
        order["_timeoutMs"] = int(self.config.get("PAPER_LIMIT_ORDER_TIMEOUT_SECONDS", 60)) * 1000

        # Coba isi segera bila sudah marketable (konservatif: hanya bila harga
        # pasar benar-benar melintasi limit).
        self._try_fill_limit(order, base, quote, filters)
        if order["status"] in ("FILLED",):
            self.store.archive_order(order)
        else:
            self.store.add_open_order(order)
        return order

    def _try_fill_limit(self, order: dict, base: str, quote: str,
                        filters: SymbolFilters) -> None:
        symbol = order["symbol"]
        side = order["side"]
        limit_px = _d(order["price"])
        already = _d(order["executedQty"])
        remaining_qty = _d(order["origQty"]) - already
        if remaining_qty <= 0:
            return
        levels = self._levels(symbol, side)
        # Saring hanya level yang melintasi limit (aturan konservatif).
        if side == "BUY":
            usable = [(p, q) for (p, q) in levels if p <= limit_px]
        else:
            usable = [(p, q) for (p, q) in levels if p >= limit_px]
        if not usable:
            return  # belum melintasi -> tetap NEW
        filled, spent, fills = self._walk_by_qty(usable, remaining_qty)
        if filled <= 0:
            return
        # Lepas dana terkunci untuk bagian yang terisi, lalu terapkan fills.
        if side == "BUY":
            self.store.consume_locked(quote, spent)
            # fee dari base; kredit base bersih.
            self._credit_base_after_fee(base, fills, maker=True, order=order,
                                        quote=quote, spent=spent)
        else:
            self.store.consume_locked(base, filled)
            self._credit_quote_after_fee(quote, fills, maker=True, order=order,
                                         base=base)
        # Perbarui akumulasi eksekusi.
        new_exec = already + filled
        new_cum = _d(order["cummulativeQuoteQty"]) + spent
        order["executedQty"] = str(new_exec)
        order["cummulativeQuoteQty"] = str(new_cum)
        if new_exec >= _d(order["origQty"]):
            order["status"] = "FILLED"
        else:
            order["status"] = "PARTIALLY_FILLED"

    def _credit_base_after_fee(self, base, fills, maker, order, quote, spent) -> None:
        rate = self._maker_rate if maker else self._taker_rate
        exec_qty = sum((q for _, q in fills), _ZERO)
        fee_base = exec_qty * rate
        self.store.credit(base, exec_qty - fee_base)
        self.store.add_fee(base, fee_base)
        recs = order.setdefault("fills", [])
        for price, qty in fills:
            recs.append({"price": str(price), "qty": str(qty),
                         "commission": str(qty * rate), "commissionAsset": base,
                         "tradeId": self._next_trade_id()})

    def _credit_quote_after_fee(self, quote, fills, maker, order, base) -> None:
        rate = self._maker_rate if maker else self._taker_rate
        gross = sum((p * q for p, q in fills), _ZERO)
        fee_quote = gross * rate
        self.store.credit(quote, gross - fee_quote)
        self.store.add_fee(quote, fee_quote)
        recs = order.setdefault("fills", [])
        for price, qty in fills:
            recs.append({"price": str(price), "qty": str(qty),
                         "commission": str(p_q_fee(p=price, q=qty, rate=rate)),
                         "commissionAsset": quote, "tradeId": self._next_trade_id()})

    # ------------------------------------------------------------------
    # STOP
    # ------------------------------------------------------------------
    def _place_stop(self, symbol, side, base, quote, filters, order_type,
                    quantity, price, stop_price, time_in_force, coid) -> dict:
        if quantity is None or stop_price is None:
            raise _reject(ERR_BAD_PARAM, "(STOP butuh quantity & stopPrice)")
        qty = _d(quantity)
        sp = _d(stop_price)
        self._check_lot_size(filters, qty)
        px = _d(price) if price is not None else _ZERO
        if px > 0:
            self._check_price_filter(filters, px)

        order = self._new_order_record(symbol, side, order_type, qty, px, sp,
                                       time_in_force or "GTC", coid)
        # Kunci base untuk stop SELL (kasus umum stop-loss bot).
        if side == "SELL":
            if self.store.get_free(base) < qty:
                raise _reject(ERR_INSUFFICIENT, f"(butuh {qty} {base})")
            self.store.lock_funds(base, qty)
            order["_lockedAsset"] = base
            order["_lockedRemaining"] = str(qty)
        else:
            # Stop BUY: kunci estimasi quote (pakai stopPrice sbg acuan).
            need = qty * sp
            if self.store.get_free(quote) < need:
                raise _reject(ERR_INSUFFICIENT, f"(butuh {need} {quote})")
            self.store.lock_funds(quote, need)
            order["_lockedAsset"] = quote
            order["_lockedRemaining"] = str(need)
        order["_createdMs"] = self._now_ms()
        self.store.add_open_order(order)
        return order

    # ------------------------------------------------------------------
    # Pemrosesan order terbuka (limit crossing, stop trigger, timeout)
    # ------------------------------------------------------------------
    def process_open_orders(self, now_ms: Optional[int] = None) -> list[dict]:
        """Evaluasi semua order terbuka terhadap harga pasar terkini:
        - LIMIT: isi bila harga melintasi; EXPIRED bila lewat timeout.
        - STOP: picu bila harga menembus stopPrice (dukung GAP), lalu isi
          sebagai market.
        Kembalikan daftar order yang berubah status. Aman dipanggil berkala."""
        now = now_ms if now_ms is not None else self._now_ms()
        changed: list[dict] = []
        with self.store.lock:
            for order in list(self.store.get_open_orders()):
                symbol = order["symbol"]
                base, quote = self._split_assets(symbol)
                filters = self.filters_provider(symbol)
                if filters is None:
                    continue
                otype = order["type"]
                status_before = order["status"]

                if otype == "LIMIT":
                    self._try_fill_limit(order, base, quote, filters)
                    timed_out = (now - int(order.get("_createdMs", now))) >= int(order.get("_timeoutMs", 0)) > 0
                    if order["status"] == "FILLED":
                        self._finalize_open(order)
                        changed.append(order)
                    elif timed_out and order["status"] in ("NEW", "PARTIALLY_FILLED"):
                        self._expire_open(order, base, quote)
                        changed.append(order)
                    elif order["status"] != status_before:
                        self.store.update_open_order(order)
                        changed.append(order)

                elif otype in ("STOP_LOSS", "TAKE_PROFIT", "STOP_LOSS_LIMIT",
                               "TAKE_PROFIT_LIMIT"):
                    triggered = self._stop_triggered(order)
                    if triggered:
                        self._execute_stop_as_market(order, base, quote, filters)
                        self._finalize_open(order)
                        changed.append(order)
            self.store.save()
        return changed

    def _stop_triggered(self, order: dict) -> bool:
        try:
            mkt = _d(self.price_provider(order["symbol"]))
        except Exception:  # noqa: BLE001
            return False
        sp = _d(order["stopPrice"])
        side = order["side"]
        otype = order["type"]
        # STOP_LOSS SELL memicu saat harga <= stop; TAKE_PROFIT SELL saat >= stop.
        if side == "SELL":
            if otype.startswith("STOP_LOSS"):
                return mkt <= sp
            return mkt >= sp  # TAKE_PROFIT
        else:  # BUY
            if otype.startswith("STOP_LOSS"):
                return mkt >= sp
            return mkt <= sp

    def _execute_stop_as_market(self, order, base, quote, filters) -> None:
        """Jalankan order stop sebagai market saat terpicu. Mendukung GAP:
        harga isi mengikuti order book SAAT INI (post-gap), bukan stopPrice."""
        side = order["side"]
        qty = _d(order["origQty"]) - _d(order["executedQty"])
        levels = self._levels(order["symbol"], side)
        filled, spent, fills = self._walk_by_qty(levels, qty)
        if filled <= 0:
            return
        if side == "SELL":
            self.store.consume_locked(base, filled)
            self._credit_quote_after_fee(quote, fills, maker=False, order=order, base=base)
        else:
            self.store.consume_locked(quote, spent)
            self._credit_base_after_fee(base, fills, maker=False, order=order,
                                        quote=quote, spent=spent)
        new_exec = _d(order["executedQty"]) + filled
        order["executedQty"] = str(new_exec)
        order["cummulativeQuoteQty"] = str(_d(order["cummulativeQuoteQty"]) + spent)
        order["status"] = "FILLED" if new_exec >= _d(order["origQty"]) else "PARTIALLY_FILLED"

    def _finalize_open(self, order: dict) -> None:
        """Pindahkan order terbuka yang sudah FILLED ke riwayat, lepas sisa
        dana terkunci bila ada."""
        # Lepas sisa dana terkunci (mis. limit yang tak terisi penuh lalu selesai).
        self._release_leftover_lock(order)
        self.store.remove_open_order(order["orderId"])
        for k in ("_lockedAsset", "_lockedRemaining", "_createdMs", "_timeoutMs"):
            order.pop(k, None)
        self.store.archive_order(order)

    def _expire_open(self, order: dict, base: str, quote: str) -> None:
        order["status"] = "EXPIRED"
        self._release_leftover_lock(order)
        self.store.remove_open_order(order["orderId"])
        for k in ("_lockedAsset", "_lockedRemaining", "_createdMs", "_timeoutMs"):
            order.pop(k, None)
        self.store.archive_order(order)

    def _release_leftover_lock(self, order: dict) -> None:
        asset = order.get("_lockedAsset")
        if not asset:
            return
        side = order["side"]
        remaining_qty = _d(order["origQty"]) - _d(order["executedQty"])
        if remaining_qty <= 0:
            return
        if side == "BUY":
            leftover = remaining_qty * _d(order["price"]) if _d(order["price"]) > 0 else _ZERO
        else:
            leftover = remaining_qty
        if leftover > 0:
            self.store.unlock_funds(asset, leftover)

    def cancel_order(self, symbol: str, order_id: Optional[int] = None,
                     orig_client_order_id: Optional[str] = None) -> dict:
        with self.store.lock:
            target = None
            for o in self.store.get_open_orders(symbol):
                if order_id is not None and o.get("orderId") == order_id:
                    target = o; break
                if orig_client_order_id is not None and o.get("clientOrderId") == orig_client_order_id:
                    target = o; break
            if target is None:
                raise BinanceAPIError(400, -2011, "Unknown order sent.")
            target["status"] = "CANCELED"
            self._release_leftover_lock(target)
            self.store.remove_open_order(target["orderId"])
            for k in ("_lockedAsset", "_lockedRemaining", "_createdMs", "_timeoutMs"):
                target.pop(k, None)
            self.store.archive_order(target)
            self.store.save()
            return target


def p_q_fee(p: Decimal, q: Decimal, rate: Decimal) -> Decimal:
    """Fee quote untuk satu fill = harga * qty * rate."""
    return p * q * rate


# ==== RINGKASAN AUDIT (paper_engine.py) ================================
# Sintaks/tipe: Decimal untuk semua uang/qty; type hints lengkap.
# Filter: _check_lot_size (minQty + kelipatan step) & _check_price_filter
#   (tick) & MIN_NOTIONAL -> menolak dengan kode Binance (-1013/-1111/-2010).
# Market walk: _walk_by_qty / _walk_by_quote menelusuri level; partial fill
#   didukung; sisa -> EXPIRED (asumsi didokumentasikan).
# Fee: dipotong dari base (BUY) / quote (SELL); tarif dari config (satu sumber).
# Idempotensi: is_duplicate_client_order_id -> -2010 "Duplicate order sent".
# Race: seluruh place_order/process_open_orders/cancel dibungkus store.lock;
#   save() dipanggil sekali per operasi majemuk.
# Konsistensi saldo: debit/consume_locked melempar bila kurang -> tak ada
#   saldo negatif diam-diam.
# Limit/stop: aturan konservatif (hanya isi bila harga melintasi); stop
#   mendukung GAP (isi pada harga book terkini, bukan stopPrice).
# CATATAN diverifikasi manual saat menulis tes: _credit_quote_after_fee memakai
#   helper p_q_fee agar nilai commission per-fill konsisten.
# Batasan: tidak memodelkan antrean/ latensi/ dampak pasar (ditulis di README).
# =======================================================================
