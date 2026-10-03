import os
from copy import deepcopy

from infrastructure.paths import PROJECT_ROOT

_MANAGED_MODE_OVERRIDE = os.environ.get("PUMP_BOT_MANAGED_MODE", "")

try:
    from dotenv import load_dotenv
    _ENV_PATH = os.path.join(str(PROJECT_ROOT), ".env")
    load_dotenv(dotenv_path=_ENV_PATH, override=True)
except ImportError:
    pass

PUMP_CONFIG = {
    "QUOTE_ASSET": "USDT",

    "MODE": "PAPER",

    "SHOW_BACKTEST_IN_LIVE": False,

    "LIVE_BASE_URL": "https://api.binance.com",
    "RATE_LIMIT_STATE_FILE": "data/binance_rate_limit_state.json",
    "RATE_LIMIT_WEIGHT_LIMIT": 6000,
    "RATE_LIMIT_SAFETY_MARGIN": 100,
    "API_KEY": os.environ.get("BINANCE_API_KEY", ""),
    "API_SECRET": os.environ.get("BINANCE_API_SECRET", ""),

    "PAPER_INITIAL_BALANCES": {"USDT": 1000.0},
    "PAPER_ACCOUNT_STATE_FILE": "data/pump_paper_account.json",
    "PAPER_DEPTH_LIMIT": 100,
    "PAPER_LIMIT_ORDER_TIMEOUT_SECONDS": 60,

    "USE_WEBSOCKET": True,
    "WS_BASE_URL": "wss://stream.binance.com:9443",
    "MAX_MARKET_DATA_AGE_SECONDS": 10.0,
    "TICKER_SNAPSHOT_TTL_SECONDS": 10,
    "WS_LAST_PRICE_OVERLAY_ENABLED": True,

    "MARKET_SCAN_INTERVAL_SECONDS": 30,
    "LOOP_INTERVAL_SECONDS": 5,
    "MARKET_DATA_WORKERS": 8,
    "MIN_QUOTE_VOLUME_USDT_24H": 2000000,
    "MARKET_DATA_INTERVAL": "5m",

    "PUMP_MIN_24H_CHANGE_PCT": 5.0,
    "PUMP_MAX_24H_CHANGE_PCT": 10.0,
    "BTC_FILTER_ENABLED": True,
    "BTC_MAX_DROP_PCT": 3.0,
    "BTC_LOOKBACK_BARS": 3,


    "USE_ATR_EXIT": True,
    "ATR_PERIOD": 14,
    "ATR_MULT_SL": 14.04,
    "ATR_MULT_TP": 17.01,
    "ATR_MULT_TRAIL": 12.13,
    "ATR_MULT_BE_TRIGGER": 5.12,
    "ATR_MULT_BE_LOCK": 0.97,
    "ATR_MULT_TRAIL_START": 5.12,

    "EXTRA_EXCLUDE_SYMBOLS": [],


    "DETECTOR_ENABLED": True,
    "DETECTOR_WEIGHT_CHANGE": 25.0,
    "DETECTOR_WEIGHT_VOLUME5M": 25.0,
    "DETECTOR_WEIGHT_ORDERBOOK": 30.0,
    "DETECTOR_WEIGHT_ATR": 20.0,
    "DETECTOR_ATR_MIN_PCT": 0.3,
    "DETECTOR_ATR_MAX_PCT": 1.2,

    "BACKTEST_INITIAL_EQUITY_USDT": 0.0,
    "BACKTEST_CACHE_ENABLED": True,
    "BACKTEST_CACHE_FILE": "data/backtest_cache.sqlite3",
    "BACKTEST_CACHE_FRESH_HOURS": 24,
    "BACKTEST_CACHE_TTL_DAYS": 30,

    "USE_TP": True,
    "TP_PCT": 80.0,
    "USE_STOP_LOSS": True,
    "USE_NATIVE_OCO": True,
    "USE_NATIVE_STOP_LOSS": True,
    "NATIVE_OCO_LIMIT_BUFFER_PCT": 0.10,
    "SL_PCT": 28.8,

    "USE_BREAKEVEN": True,
    "BE_TRIGGER_PCT": 19.2,
    "BE_LOCK_PCT": 3.2,
    "USE_TRAILING": True,
    "TRAILING_START_PCT": 28.8,
    "TRAILING_STEP_PCT": 14.4,

    "TAKER_FEE_PCT": 0.1,
    "MAKER_FEE_PCT": 0.1,
    "USE_BNB_FEE_DISCOUNT": True,
    "USE_EQUITY_STOP": True,
    "MAX_DRAWDOWN_PERCENT": 12.0,
    "USE_DAILY_STOP": True,
    "MAX_DAILY_LOSS_PERCENT": 5.0,
    "DAILY_PROFIT_TARGET_PERCENT": 15.0,
    "CLOSE_ALL_AT_LIMIT": True,
    "DD_COOLDOWN_HOURS": 24,

    "MAX_CONSECUTIVE_ERRORS": 20,
    "SUPERVISOR_AUTO_RESTART": True,
    "SUPERVISOR_MAX_RESTARTS": 5,
    "SUPERVISOR_RESTART_WINDOW_SECONDS": 300,
    "SUPERVISOR_RESTART_BACKOFF_SECONDS": 5,

    "STATE_FILE": "data/pump_bot_state.json",
    "LOG_FILE": "logs/pump_bot.log",
    "HEARTBEAT_INTERVAL_SECONDS": 300,
    "CONTROL_FILE": "data/pump_bot_control.json",

    "USE_DUST_SWEEP": True,

    "TOP_N_CANDIDATES_TO_CONFIRM": 15,
    "CONFIRM_INTERVAL": "5m",
    "CONFIRM_LOOKBACK_BARS": 60,
    "ROLLING_VOLUME_FILTER_ENABLED": True,
    "ROLLING_VOLUME_LOOKBACK_BARS": 20,
    "ROLLING_VOLUME_SURGE_MULT": 2.0,
    "ROLLING_VOLUME_CONFIRMATION_BARS": 1,
    "MIN_LISTING_AGE_DAYS": 7,
    "USE_RISK_PERCENT": True,
    "RISK_PERCENT": 100.0,
    "POSITION_SIZE_USDT": 5.0,
    "BACKTEST_ENTRY_SPREAD_PCT": 0.10,
    "BACKTEST_SLIPPAGE_PCT": 0.05,
    "BACKTEST_ENTRY_DELAY_BARS": 1,
    "MAX_POSITION_USDT": 100,
    "BALANCE_BUFFER_PCT": 0.5,
    "MAX_SPREAD_PCT": 0.25,
    "MAX_CHASE_PCT": 1.5,
    "DEPTH_FILTER_ENABLED": True,
    "DEPTH_RANGE_PCT": 0.5,
    "DEPTH_MIN_ASK_NOTIONAL_MULT": 10.0,
    "ORDERBOOK_FILTER_ENABLED": True,
    "ORDERBOOK_LEVELS": 10,
    "ORDERBOOK_MIN_BID_ASK_RATIO": 0.8,
    "SELL_WALL_RANGE_PCT": 1.0,
    "SELL_WALL_MAX_SHARE_PCT": 30.0,
    "ORDERBOOK_DEPTH_LIMIT": 100,
    "COOLDOWN_MINUTES_AFTER_CLOSE": 5,
    "MIN_SECONDS_BETWEEN_TRADES": 60,

}

PUMP_DEFAULTS = deepcopy(PUMP_CONFIG)
PUMP_DEFAULTS["BASE_URL"] = PUMP_DEFAULTS["LIVE_BASE_URL"]
CONFIG_LOAD_ERRORS: list[str] = []
MODE_SOURCE = "default"


def _mode_from_environment(default: str) -> tuple[str, str, list[str]]:
    managed = str(_MANAGED_MODE_OVERRIDE or "").strip()
    if managed:
        return managed, "managed-parent", []

    primary = str(os.environ.get("BOT_MODE", "") or "").strip()
    legacy = str(os.environ.get("MODE", "") or "").strip()
    errors: list[str] = []
    if primary and legacy and primary.upper() != legacy.upper():
        errors.append(
            f"BOT_MODE={primary!r} berbeda dari MODE={legacy!r}. "
            "Sisakan satu nilai mode yang konsisten di .env."
        )
    if primary:
        return primary, ".env:BOT_MODE", errors
    if legacy:
        return legacy, ".env:MODE", errors
    return default, "default:PAPER", errors


def _load_runtime_layers(explicit_mode: str | None = None) -> None:
    global MODE_SOURCE
    from config.settings_schema import load_mode_override, validate_candidate

    cfg = deepcopy(PUMP_DEFAULTS)
    cfg["API_KEY"] = os.environ.get("BINANCE_API_KEY", "")
    cfg["API_SECRET"] = os.environ.get("BINANCE_API_SECRET", "")
    errors: list[str] = []
    if explicit_mode is None:
        active_mode, mode_source, mode_errors = _mode_from_environment(
            str(cfg.get("MODE", "PAPER"))
        )
        errors.extend(mode_errors)
    else:
        active_mode = explicit_mode
        mode_source = "explicit"
    MODE_SOURCE = mode_source
    cfg["MODE"] = active_mode

    normalized = str(active_mode).strip().upper()
    if normalized in ("PAPER", "LIVE"):
        override, override_errors = load_mode_override(normalized)
        errors.extend(override_errors)
        cfg.update(override)
        cfg["MODE"] = normalized
        cleaned, validation_errors, _ = validate_candidate(cfg, normalized)
        if validation_errors:
            errors.extend(
                f"{key}: {message}" for key, message in validation_errors.items()
            )
        else:
            cfg = cleaned
    else:
        errors.append(
            f"Mode runtime tidak valid: {active_mode!r}. Hanya PAPER atau LIVE yang diizinkan."
        )

    PUMP_CONFIG.clear()
    PUMP_CONFIG.update(cfg)
    CONFIG_LOAD_ERRORS.clear()
    CONFIG_LOAD_ERRORS.extend(dict.fromkeys(errors))


_load_runtime_layers()


from config.settings_schema import VALID_MODES


class InvalidModeError(ValueError):
    pass


def get_mode(config: dict = None) -> str:
    cfg = PUMP_CONFIG if config is None else config
    mode = str(cfg.get("MODE", "PAPER")).strip().upper()
    return mode if mode in VALID_MODES else "PAPER"


def get_mode_source() -> str:
    return MODE_SOURCE


def require_valid_mode(config: dict = None) -> str:
    cfg = PUMP_CONFIG if config is None else config
    raw = cfg.get("MODE", None)
    mode = str(raw).strip().upper() if raw is not None else ""
    if mode not in VALID_MODES:
        raise InvalidModeError(
            f"MODE tidak valid: {raw!r}. Nilai yang diizinkan hanya "
            f"{', '.join(VALID_MODES)}. Perbaiki BOT_MODE di file .env. "
            "Bot TIDAK akan berjalan dengan mode yang tidak dikenal demi keamanan."
        )
    return mode


def is_paper(config: dict = None) -> bool:
    return get_mode(config) == "PAPER"


def backtest_enabled(config: dict = None) -> bool:
    if is_paper(config):
        return True
    cfg = PUMP_CONFIG if config is None else config
    raw = cfg.get("SHOW_BACKTEST_IN_LIVE", False)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "1", "yes", "ya", "on")


def get_base_url(config: dict = None) -> str:
    cfg = PUMP_CONFIG if config is None else config
    return cfg.get("LIVE_BASE_URL", "https://api.binance.com")


def use_websocket(config: dict = None) -> bool:
    cfg = PUMP_CONFIG if config is None else config
    raw = cfg.get("USE_WEBSOCKET", True)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "1", "yes", "ya", "on")


def get_paper_account_file(config: dict = None) -> str:
    cfg = PUMP_CONFIG if config is None else config
    return _mode_filename(
        str(cfg.get("PAPER_ACCOUNT_STATE_FILE", "data/pump_paper_account.json")),
        get_mode(cfg),
    )


def _mode_filename(base: str, mode: str) -> str:
    if not base:
        return base
    tag = mode.lower()
    root, ext = os.path.splitext(base)
    for old_tag in ("_paper", "_live", "_testnet"):
        if root.lower().endswith(old_tag):
            root = root[: -len(old_tag)]
            break
    return f"{root}_{tag}{ext}"


def get_state_file(config: dict = None) -> str:
    cfg = PUMP_CONFIG if config is None else config
    return _mode_filename(str(cfg.get("STATE_FILE", "data/pump_bot_state.json")), get_mode(cfg))


def get_log_file(config: dict = None) -> str:
    cfg = PUMP_CONFIG if config is None else config
    return _mode_filename(str(cfg.get("LOG_FILE", "logs/pump_bot.log")), get_mode(cfg))


def get_control_file(config: dict = None) -> str:
    cfg = PUMP_CONFIG if config is None else config
    return _mode_filename(str(cfg.get("CONTROL_FILE", "data/pump_bot_control.json")), get_mode(cfg))


_PROJECT_ROOT = str(PROJECT_ROOT)


def _runtime_path(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(_PROJECT_ROOT, path)


def _finalize_config_dict(cfg: dict) -> dict:
    cfg["BASE_URL"] = get_base_url(cfg)
    cfg["STATE_FILE"] = _runtime_path(get_state_file(cfg))
    cfg["LOG_FILE"] = _runtime_path(get_log_file(cfg))
    cfg["CONTROL_FILE"] = _runtime_path(get_control_file(cfg))
    cfg["PAPER_ACCOUNT_STATE_FILE"] = _runtime_path(get_paper_account_file(cfg))
    cfg["RATE_LIMIT_STATE_FILE"] = _runtime_path(
        str(cfg.get("RATE_LIMIT_STATE_FILE", "data/binance_rate_limit_state.json"))
    )
    cfg["BACKTEST_CACHE_FILE"] = _runtime_path(
        str(cfg.get("BACKTEST_CACHE_FILE", "data/backtest_cache.sqlite3"))
    )
    return cfg


def default_config_for_mode(mode: str) -> dict:
    cfg = deepcopy(PUMP_DEFAULTS)
    cfg["MODE"] = str(mode).strip().upper()
    cfg["API_KEY"] = os.environ.get("BINANCE_API_KEY", "")
    cfg["API_SECRET"] = os.environ.get("BINANCE_API_SECRET", "")
    return _finalize_config_dict(cfg)


def build_config_for_mode(mode: str, *, validate: bool = True) -> tuple[dict, list[str]]:
    from config.settings_schema import load_mode_override, validate_candidate

    raw = str(mode).strip().upper()
    cfg = default_config_for_mode(raw)
    errors: list[str] = []
    if raw in VALID_MODES:
        override, errors = load_mode_override(raw)
        cfg.update(override)
        cfg["MODE"] = raw
        if validate:
            cleaned, validation_errors, _ = validate_candidate(cfg, raw)
            if validation_errors:
                errors.extend(
                    f"{key}: {message}" for key, message in validation_errors.items()
                )
            else:
                cfg = cleaned
        _finalize_config_dict(cfg)
    return cfg, errors


_finalize_config_dict(PUMP_CONFIG)
PUMP_DEFAULTS["BASE_URL"] = PUMP_DEFAULTS["LIVE_BASE_URL"]


def detector_enabled(config: dict = None) -> bool:
    cfg = PUMP_CONFIG if config is None else config
    raw = cfg.get("DETECTOR_ENABLED", False)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "1", "yes", "ya", "on")


def get_taker_fee_pct(config: dict = None) -> float:
    cfg = PUMP_CONFIG if config is None else config
    fee = float(cfg.get("TAKER_FEE_PCT", 0.1))
    if cfg.get("USE_BNB_FEE_DISCOUNT"):
        fee *= 0.75
    return fee
