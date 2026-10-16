"""End-to-end CLI tests against a *fake easytrader client*.

This is the strongest evidence available without a real brokerage client: it
drives the real `easytrader_ths` adapter (not the mock broker) through the real
CLI, with a stand-in object that mimics easytrader 0.23.7's surface —
including the fact that balance/position/today_entrusts are properties.

If the adapter's assumption about easytrader's API were wrong (e.g. calling a
property like a method), these tests would fail exactly the way a real client
would fail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from etfa.cli import main


class FakeEasytraderClient:
    """Mimics easytrader 0.23.7 ClientTrader as observed by introspection."""

    def __init__(self, cash=50000.0, price=1.092, held=18000):
        self.cash = cash
        self.price = price
        self.held = held
        self.placed = []

    # --- properties (NOT methods) ---
    @property
    def balance(self):
        return {"资金余额": f"{self.cash:.2f}", "可用金额": f"{self.cash:.2f}",
                "最新市值": f"{self.held * self.price:.2f}",
                "总资产": f"{self.cash + self.held * self.price:.2f}",
                "冻结金额": "0"}

    @property
    def position(self):
        if self.held <= 0:
            return []
        return [{"证券代码": "513050", "证券名称": "中概互联网ETF易方达",
                 "股票余额": str(self.held), "可用余额": str(self.held),
                 "参考成本价": "1.050", "市价": f"{self.price:.3f}"}]

    @property
    def today_entrusts(self):
        return [{"委托编号": f"E{i:04d}", "证券代码": c, "操作": "买入" if s == "buy" else "卖出",
                 "委托价格": f"{p:.3f}", "委托数量": str(q), "成交数量": str(q),
                 "委托状态": "已报"}
                for i, (s, c, p, q) in enumerate(self.placed, start=1)]

    # --- methods ---
    def buy(self, security, price, amount, **kwargs):
        self.placed.append(("buy", security, price, amount))
        return {"entrust_no": f"E{len(self.placed):04d}"}

    def sell(self, security, price, amount, **kwargs):
        self.placed.append(("sell", security, price, amount))
        return {"entrust_no": f"E{len(self.placed):04d}"}

    def cancel_entrust(self, entrust_no):
        return {"message": "success"}


def live_config(tmp_path: Path) -> Path:
    cfg = {
        "broker": {"type": "easytrader_ths", "clienttype": "ths", "exe_path": "C:\\fake\\xiadan.exe"},
        "trading": {
            "allowed_codes": ["513050"],
            "t0_codes": ["513050"],
            "min_order_amount": 1000.0,
            "max_order_amount": 10000.0,
            "max_daily_amount": 30000.0,
            "max_position_pct": 90.0,
        },
        "paths": {"var": str(tmp_path / "var")},
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def run_cli(*argv, capsys):
    with pytest.raises(SystemExit) as ei:
        main(list(argv))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    return ei.value.code or 0, payload


@pytest.fixture()
def armed(tmp_path, monkeypatch):
    """Patch the adapter factory so the live adapter wraps a fake client."""
    import etfa.cli as climod
    from etfa.broker.easytrader_ths import EasytraderThsBroker

    client = FakeEasytraderClient()

    def factory(cfg):
        b = EasytraderThsBroker(cfg, trader=client)
        b._connected = True
        return b

    monkeypatch.setattr(climod, "get_adapter", factory)
    return client


def test_live_adapter_reads_balance_and_positions(capsys, tmp_path, armed):
    cfg = live_config(tmp_path)
    code, out = run_cli("--config", str(cfg), "balance", capsys=capsys)
    assert out["ok"] is True, out
    assert out["balance"]["cash"] == 50000.0
    assert out["balance"]["total"] == 69656.0

    code, out = run_cli("--config", str(cfg), "positions", capsys=capsys)
    assert out["ok"] is True
    assert out["positions"][0]["code"] == "513050"
    assert out["positions"][0]["qty"] == 18000


def test_live_adapter_buys_through_positional_args(capsys, tmp_path, armed):
    cfg = live_config(tmp_path)
    code, out = run_cli("--config", str(cfg), "buy", "513050", "--qty", "2800",
                        "--force", "--reason", "live-path test", capsys=capsys)
    assert out["ok"] is True, out
    assert out["order"]["id"] == "E0001"
    # (security, price, amount) — argument order matters.
    # Price is `last` (1.092), not ask1: easytrader exposes no order book, so the
    # live path has no bid/ask. See docs/COMPATIBILITY.md.
    assert armed.placed == [("buy", "513050", 1.092, 2800)]


def test_live_adapter_quote_comes_from_held_position(capsys, tmp_path, armed):
    cfg = live_config(tmp_path)
    code, out = run_cli("--config", str(cfg), "quote", "513050", capsys=capsys)
    assert out["ok"] is True
    assert out["quotes"]["513050"]["last"] == 1.092

    # an un-held code has no quote on this path, and must be reported as missing
    code, out = run_cli("--config", str(cfg), "quote", "159920", capsys=capsys)
    assert out["ok"] is True
    assert out["missing"] == ["159920"]


def test_strategy_plan_needs_explicit_price_without_a_quote_feed(capsys, tmp_path, armed):
    """Not held -> no quote -> strategy must be told the price explicitly."""
    cfg = live_config(tmp_path)
    # hold nothing so there is no price source at all
    armed.held = 0

    code, out = run_cli("--config", str(cfg), "plan", "grid", "--code", "513050",
                        "--base", "1.0", "--spacing", "3", "--qty", "1000", capsys=capsys)
    assert out["ok"] is True
    assert out["intents"] == []          # nothing to reason about: no price

    # with --price it can plan
    code, out = run_cli("--config", str(cfg), "plan", "grid", "--code", "513050",
                        "--base", "1.0", "--spacing", "3", "--qty", "1000",
                        "--price", "0.965", capsys=capsys)
    assert out["ok"] is True
    assert len(out["intents"]) == 1
    assert out["intents"][0]["side"] == "buy"
    assert armed.placed == []            # plan never orders


def test_live_orders_and_cancel(capsys, tmp_path, armed):
    cfg = live_config(tmp_path)
    run_cli("--config", str(cfg), "buy", "513050", "--qty", "2800", "--force", capsys=capsys)
    code, out = run_cli("--config", str(cfg), "orders", capsys=capsys)
    assert out["ok"] is True
    assert out["count"] == 1
    assert out["orders"][0]["side"] == "buy"

    code, out = run_cli("--config", str(cfg), "cancel", "E0001", capsys=capsys)
    assert out["ok"] is True
    assert out["cancelled"] == ["E0001"]


def test_live_risk_guard_still_applies(capsys, tmp_path, armed):
    """The guard must not be bypassed just because a real client is attached."""
    cfg = live_config(tmp_path)
    code, out = run_cli("--config", str(cfg), "buy", "513050", "--qty", "100000",
                        "--force", capsys=capsys)
    assert out["ok"] is False
    assert out["error"] == "risk_rejected"
    assert armed.placed == []            # nothing reached the client
