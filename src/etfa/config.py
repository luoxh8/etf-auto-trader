"""Configuration loading and validation.

Design rule: **no credentials live in the config file.** The default adapter
(``easytrader_ths``) attaches to a client the user has already logged into
by hand, so nothing secret needs persisting. If you ever use an adapter that
needs a password, it is read from the environment variable named in
``broker.password_env`` — never written to disk.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import ConfigError

DEFAULT_CONFIG_RELPATH = "config/config.json"
MAX_QTY_PER_ORDER = 1_000_000


@dataclass
class BrokerConfig:
    type: str = "mock"                 # mock | easytrader_ths | qmt
    clienttype: str = "ths"            # easytrader clienttype: ths | universal_client
    exe_path: str = ""                 # path to xiadan.exe / hexin.exe
    user: str = ""                     # only for universal_client
    password_env: str = "ETFA_BROKER_PASSWORD"
    connect_timeout: float = 20.0


@dataclass
class TradingConfig:
    allowed_codes: List[str] = field(default_factory=list)
    t0_codes: List[str] = field(default_factory=list)
    min_order_amount: float = 0.0      # reject notional below this
    max_order_amount: float = 100_000.0
    max_daily_amount: float = 200_000.0
    max_position_pct: float = 100.0    # cap single-code market value as % of total assets
    allow_outside_hours: bool = False
    holidays_file: str = ""


@dataclass
class Paths:
    var: str = "var"


@dataclass
class Config:
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    paths: Paths = field(default_factory=Paths)
    _root: Optional[Path] = None

    @property
    def root(self) -> Path:
        return self._root or Path.cwd()

    def var_dir(self) -> Path:
        p = Path(self.paths.var)
        if not p.is_absolute():
            p = self.root / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    def audit_path(self) -> Path:
        return self.var_dir() / "audit.jsonl"

    def state_path(self, name: str) -> Path:
        return self.var_dir() / name

    def to_public_dict(self) -> Dict[str, Any]:
        """Config with no secret material (there should be none)."""
        return {
            "broker": {
                "type": self.broker.type,
                "clienttype": self.broker.clienttype,
                "exe_path": self.broker.exe_path,
                "user": self.broker.user,
                "password_env": self.broker.password_env,
            },
            "trading": {
                "allowed_codes": self.trading.allowed_codes,
                "t0_codes": self.trading.t0_codes,
                "min_order_amount": self.trading.min_order_amount,
                "max_order_amount": self.trading.max_order_amount,
                "max_daily_amount": self.trading.max_daily_amount,
                "max_position_pct": self.trading.max_position_pct,
                "allow_outside_hours": self.trading.allow_outside_hours,
                "holidays_file": self.trading.holidays_file,
            },
            "paths": {"var": self.paths.var},
            "root": str(self.root),
        }


def _from_dict(raw: Dict[str, Any], root: Path) -> Config:
    broker_raw = raw.get("broker") or {}
    trading_raw = raw.get("trading") or {}
    paths_raw = raw.get("paths") or {}

    broker = BrokerConfig(
        type=str(broker_raw.get("type", "mock")),
        clienttype=str(broker_raw.get("clienttype", "ths")),
        exe_path=str(broker_raw.get("exe_path", "")),
        user=str(broker_raw.get("user", "")),
        password_env=str(broker_raw.get("password_env", "ETFA_BROKER_PASSWORD")),
        connect_timeout=float(broker_raw.get("connect_timeout", 20.0)),
    )
    trading = TradingConfig(
        allowed_codes=[str(c).strip() for c in (trading_raw.get("allowed_codes") or [])],
        t0_codes=[str(c).strip() for c in (trading_raw.get("t0_codes") or [])],
        min_order_amount=float(trading_raw.get("min_order_amount", 0.0)),
        max_order_amount=float(trading_raw.get("max_order_amount", 100_000.0)),
        max_daily_amount=float(trading_raw.get("max_daily_amount", 200_000.0)),
        max_position_pct=float(trading_raw.get("max_position_pct", 100.0)),
        allow_outside_hours=bool(trading_raw.get("allow_outside_hours", False)),
        holidays_file=str(trading_raw.get("holidays_file", "")),
    )
    paths = Paths(var=str(paths_raw.get("var", "var")))
    return Config(broker=broker, trading=trading, paths=paths, _root=root)


def find_config(explicit: Optional[str] = None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = Path.cwd() / p
        return p
    # walk up from cwd looking for config/config.json
    here = Path.cwd().resolve()
    for base in [here, *here.parents]:
        cand = base / DEFAULT_CONFIG_RELPATH
        if cand.exists():
            return cand
    return here / DEFAULT_CONFIG_RELPATH


def load_config(explicit: Optional[str] = None) -> Config:
    path = find_config(explicit)
    if not path.exists():
        raise ConfigError(
            f"config not found at {path}. Copy config/config.example.json to config/config.json first."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config {path} must be a JSON object")
    cfg = _from_dict(raw, path.parent.parent if path.parent.name == "config" else path.parent)
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if cfg.broker.type not in ("mock", "easytrader_ths", "qmt"):
        raise ConfigError(f"unknown broker.type '{cfg.broker.type}'")
    if cfg.broker.type == "easytrader_ths":
        if cfg.broker.clienttype not in ("ths", "universal_client"):
            raise ConfigError("broker.clienttype must be 'ths' or 'universal_client'")
    if cfg.trading.min_order_amount and cfg.trading.max_order_amount:
        if cfg.trading.min_order_amount > cfg.trading.max_order_amount:
            raise ConfigError("trading.min_order_amount > max_order_amount")
    if cfg.trading.max_position_pct <= 0 or cfg.trading.max_position_pct > 100:
        raise ConfigError("trading.max_position_pct must be in (0, 100]")
    for code in cfg.trading.allowed_codes:
        if not code.isdigit() or len(code) != 6:
            raise ConfigError(f"allowed_codes entry '{code}' is not a 6-digit code")


def broker_password(cfg: Config) -> Optional[str]:
    """Password for adapters that need one, sourced only from the environment."""
    return os.environ.get(cfg.broker.password_env) or None
