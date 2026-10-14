def update_equity_controls(state: dict, equity: float, config: dict) -> bool:
    if state.get("peak_equity") is None or equity > state["peak_equity"]:
        state["peak_equity"] = equity

    peak = state["peak_equity"]
    dd_pct = (peak - equity) / peak * 100.0 if peak else 0.0
    dd_pct = max(dd_pct, 0.0)
    if dd_pct > float(state.get("max_dd_pct") or 0.0):
        state["max_dd_pct"] = dd_pct

    if (
        config["USE_EQUITY_STOP"]
        and not state.get("dd_stopped")
        and state["peak_equity"]
    ):
        if dd_pct >= config["MAX_DRAWDOWN_PERCENT"]:
            state["dd_stopped"] = True
            state["dd_stop_until"] = (
                state_mod.now_ms() + config["DD_COOLDOWN_HOURS"] * 3600 * 1000
            )
            state["dd_reset_count"] = int(state.get("dd_reset_count") or 0) + 1
            logger.critical(
                "STOP DRAWDOWN: turun %.2f%% dari puncak equity (max DD di-reset ke 0). "
                "Entry baru dijeda %d jam.",
                dd_pct,
                config["DD_COOLDOWN_HOURS"],
            )
            state["max_dd_pct"] = 0.0
            state["peak_equity"] = equity

    if state.get("dd_stopped"):
        deadline = state.get("dd_stop_until") or 0
        if not config.get("USE_EQUITY_STOP"):
            state["dd_stopped"] = False
            state["dd_stop_until"] = 0
            state["peak_equity"] = equity
            logger.info(
                "USE_EQUITY_STOP nonaktif: status stop drawdown yang tersisa dilepas."
            )
        elif not deadline:
            state["dd_stopped"] = False
            state["dd_stop_until"] = 0
            state["peak_equity"] = equity
            logger.warning(
                "Status stop drawdown tidak punya batas waktu (state lama atau rusak). "
                "Status dilepas dan puncak equity dihitung ulang dari nilai sekarang."
            )
        elif state_mod.now_ms() >= deadline:
            state["dd_stopped"] = False
            state["dd_stop_until"] = 0
            state["peak_equity"] = equity
            logger.info("Cooldown drawdown selesai. Entry baru diaktifkan lagi.")

    return bool(state.get("dd_stopped"))
