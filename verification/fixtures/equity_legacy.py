def get_equity(
    client: ExchangeClient,
    config: dict,
    state: dict,
    position_price: float | None = None,
) -> "float | None":
    quote_asset = str(config.get("QUOTE_ASSET") or "").strip().upper()
    if not quote_asset:
        logger.warning("Ekuitas akun tidak dapat dihitung: aset kuotasi kosong.")
        return None

    def unverified(reason: str) -> None:
        logger.warning(
            "Ekuitas seluruh akun tidak dapat diverifikasi (%s). "
            "Evaluasi batas risiko dilewati satu iterasi.",
            reason,
        )
        return None

    try:
        account = client.get_account()
        if str(config.get("MODE", "PAPER")).upper() == "LIVE":
            _validate_live_account_snapshot(account, quote_asset)
    except Exception as exc:
        return unverified(
            f"snapshot account tidak tersedia/valid: {type(exc).__name__}"
        )

    if not isinstance(account, dict):
        return unverified("respons account bukan object")
    raw_balances = account.get("balances")
    if not isinstance(raw_balances, list):
        return unverified("daftar balances tidak tersedia")

    balances: dict[str, float] = {}
    for item in raw_balances:
        if not isinstance(item, dict):
            return unverified("entri balance bukan object")
        asset = str(item.get("asset") or "").strip().upper()
        if not asset or asset in balances:
            return unverified(f"aset kosong atau duplikat: {asset or '<kosong>'}")
        try:
            free = float(item.get("free"))
            locked = float(item.get("locked"))
        except (TypeError, ValueError, OverflowError):
            return unverified(f"saldo {asset} bukan angka")
        if (
            not math.isfinite(free)
            or not math.isfinite(locked)
            or free < 0
            or locked < 0
        ):
            return unverified(f"saldo {asset} tidak valid")
        total = free + locked
        if not math.isfinite(total):
            return unverified(f"saldo total {asset} tidak finite")
        balances[asset] = total

    equity_parts = [balances.get(quote_asset, 0.0)]
    active_symbol = str(state.get("current_symbol") or "").strip().upper()
    active_price = None
    if position_price is not None:
        try:
            candidate = float(position_price)
        except (TypeError, ValueError, OverflowError):
            candidate = 0.0
        if math.isfinite(candidate) and candidate > 0:
            active_price = candidate

    assets_to_price = []
    for asset, total in balances.items():
        if asset == quote_asset or total <= 0:
            continue
        symbol = f"{asset}{quote_asset}"
        if symbol == active_symbol and active_price is not None:
            equity_parts.append(total * active_price)
        else:
            assets_to_price.append((asset, symbol, total))

    if assets_to_price:
        try:
            tickers = client.get_ticker_24hr_all()
        except Exception as exc:
            return unverified(f"snapshot ticker tidak tersedia: {type(exc).__name__}")
        if not isinstance(tickers, list):
            return unverified("snapshot ticker bukan list")

        ticker_by_symbol = {}
        duplicate_symbols = set()
        for ticker in tickers:
            if not isinstance(ticker, dict):
                continue
            symbol = str(ticker.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            if symbol in ticker_by_symbol:
                duplicate_symbols.add(symbol)
            else:
                ticker_by_symbol[symbol] = ticker

        try:
            ticker_ttl = float(config.get("TICKER_SNAPSHOT_TTL_SECONDS", 30.0) or 0.0)
            max_market_age = float(
                config.get("MAX_MARKET_DATA_AGE_SECONDS", 10.0) or 0.0
            )
        except (TypeError, ValueError, OverflowError):
            return unverified("batas umur snapshot ticker bukan angka")
        if (
            not math.isfinite(ticker_ttl)
            or not math.isfinite(max_market_age)
            or ticker_ttl < 0
            or max_market_age < 0
        ):
            return unverified("batas umur snapshot ticker tidak valid")
        max_age_seconds = min(
            ticker_ttl + max_market_age, max(60.0, max_market_age)
        )
        max_age_ms = int(max_age_seconds * 1000.0)
        now_ms = int(time.time() * 1000.0)

        for asset, symbol, amount in assets_to_price:
            if symbol in duplicate_symbols:
                return unverified(f"ticker {symbol} duplikat")
            ticker = ticker_by_symbol.get(symbol)
            if ticker is None:
                return unverified(f"harga bid {symbol} tidak tersedia")
            try:
                bid = float(ticker.get("bidPrice"))
                close_time_ms = int(ticker.get("closeTime"))
            except (TypeError, ValueError, OverflowError):
                return unverified(f"bid/timestamp {symbol} bukan angka")
            if not math.isfinite(bid) or bid <= 0 or close_time_ms <= 0:
                return unverified(f"bid/timestamp {symbol} tidak valid")
            if abs(now_ms - close_time_ms) > max_age_ms:
                return unverified(f"snapshot {symbol} kedaluwarsa")
            value = amount * bid
            if not math.isfinite(value):
                return unverified(f"nilai saldo {asset} tidak finite")
            equity_parts.append(value)

    try:
        equity = math.fsum(equity_parts)
    except OverflowError:
        return unverified("total ekuitas overflow")
    if not math.isfinite(equity) or equity < 0:
        return unverified("total ekuitas tidak valid")
    return equity


