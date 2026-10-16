"""Broker adapters.

Importing this package is cheap and never imports a trading dependency:
``get_adapter`` performs the live import lazily so that the CLI, mock runs
and tests all work on a bare interpreter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..config import Config
from ..errors import ConfigError
from .base import BrokerAdapter
from .mock import MockBroker


def get_adapter(cfg: Config) -> BrokerAdapter:
    kind = cfg.broker.type
    if kind == "mock":
        # Persist the fake book in var/ so a sequence of CLI calls behaves like
        # one continuous account; seed it once from config/mock_seed.json if present.
        persist = cfg.state_path("mock-account.json")
        seed_path = cfg.root / "config" / "mock_seed.json"
        seed = None
        if seed_path.exists() and not persist.exists():
            try:
                seed = json.loads(seed_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                seed = None
        return MockBroker(seed, persist_path=str(persist))
    if kind == "easytrader_ths":
        from .easytrader_ths import EasytraderThsBroker

        return EasytraderThsBroker(cfg)
    if kind == "qmt":
        from .qmt import QmtBroker

        return QmtBroker(cfg)
    raise ConfigError(f"unknown broker.type '{kind}'")


__all__ = ["BrokerAdapter", "MockBroker", "get_adapter"]
