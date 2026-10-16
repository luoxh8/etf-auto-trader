"""End-to-end CLI tests against the mock broker.

These exercise the real argparse surface and the real risk guard, so they are
the regression net for "can an AI drive this correctly".
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from etfa.cli import main

REPO = Path(__file__).resolve().parents[1]


def run_cli(*argv, capsys=None):
    """Invoke the CLI in-process and parse its single JSON line."""
    with pytest.raises(SystemExit) as ei:
        main(list(argv))
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    return ei.value.code or 0, payload


def write_config(tmp_path: Path, broker_type: str = "mock") -> Path:
    cfg = {
        "broker": {"type": broker_type},
        "trading": {
            "allowed_codes": ["513050"],
            "min_order_amount": 100.0,
            "max_order_amount": 10000.0,
            "max_daily_amount": 30000.0,
            "max_position_pct": 90.0,
        },
        "paths": {"var": str(tmp_path / "var")},
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def test_status_runs_without_a_broker(capsys, tmp_path):
    cfg = write_config(tmp_path)
    code, out = run_cli("--config", str(cfg), "status", capsys=capsys)
    assert out["ok"] is True
    assert out["broker_type"] == "mock"
    assert "session" in out


def test_doctor_reports_checks(capsys, tmp_path):
    cfg = write_config(tmp_path)
    code, out = run_cli("--config", str(cfg), "doctor", capsys=capsys)
    assert out["ok"] is True
    assert any(c["check"] == "config" for c in out["checks"])
    # readiness is about setup; the market being closed is reported separately
    assert out["ready"] is True
    assert "market_open" in out
    assert "can_trade_now" in out


def test_mock_buy_and_positions_roundtrip(capsys, tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    monkeypatch.setenv("ETFA_TEST", "1")
    # seed a deterministic book
    from etfa.broker.mock import MockBroker
    seed = json.loads((REPO / "tests" / "mock_seed.json").read_text(encoding="utf-8"))

    # the CLI builds its own adapter; force it to use a connected mock by
    # monkeypatching the factory it imports.
    import etfa.cli as climod
    from etfa.broker import mock as mockmod

    shared = MockBroker(seed)
    shared.connect()
    monkeypatch.setattr(climod, "get_adapter", lambda cfg: shared)

    code, out = run_cli("--config", str(cfg), "--broker", "mock",
                        "buy", "513050", "--qty", "2800", "--force", capsys=capsys)
    assert out["ok"] is True, out
    assert out["submitted"] is True
    assert out["order"]["qty"] == 2800

    code, out = run_cli("--config", str(cfg), "--broker", "mock", "positions", capsys=capsys)
    assert out["ok"] is True
    assert out["positions"][0]["qty"] == 20800  # 18000 + 2800


def test_buy_outside_whitelist_is_rejected_json(capsys, tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    import etfa.cli as climod
    from etfa.broker.mock import MockBroker

    shared = MockBroker({"cash": 100000})
    shared.connect()
    monkeypatch.setattr(climod, "get_adapter", lambda cfg: shared)

    code, out = run_cli("--config", str(cfg), "--broker", "mock",
                        "buy", "159915", "--qty", "100", "--force", capsys=capsys)
    assert out["ok"] is False
    assert out["error"] == "risk_rejected"
    assert code == 2


def test_plan_never_submits(capsys, tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    import etfa.cli as climod
    from etfa.broker.mock import MockBroker

    shared = MockBroker({"cash": 100000, "prices": {"513050": 0.965}})
    shared.connect()
    monkeypatch.setattr(climod, "get_adapter", lambda cfg: shared)

    code, out = run_cli("--config", str(cfg), "--broker", "mock", "plan", "grid",
                        "--code", "513050", "--base", "1.0", "--spacing", "3",
                        "--qty", "1000", capsys=capsys)
    assert out["ok"] is True
    assert out["strategy"] == "grid"
    assert len(out["intents"]) == 1
    assert all(d["submitted"] is False for d in out["decisions"])
    assert shared.calls == []  # nothing hit the broker


def test_grid_ladder_is_pure(capsys, tmp_path):
    cfg = write_config(tmp_path)
    code, out = run_cli("--config", str(cfg), "grid", "ladder",
                        "--base", "1.092", "--spacing", "3", capsys=capsys)
    assert out["ok"] is True
    lad = out["ladder"]
    assert len(lad["buy_levels"]) == 4 and len(lad["sell_levels"]) == 4


def test_broker_rejection_is_reported_as_failure(capsys, tmp_path, monkeypatch):
    """If the broker refuses (e.g. no cash), the CLI must not claim success."""
    # permissive risk config so the guard lets it through and the *broker* is
    # the thing that refuses
    cfg = {
        "broker": {"type": "mock"},
        "trading": {"allowed_codes": ["513050"], "min_order_amount": 0.0,
                    "max_order_amount": 1e9, "max_daily_amount": 1e9,
                    "max_position_pct": 100.0},
        "paths": {"var": str(tmp_path / "var")},
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")

    import etfa.cli as climod
    from etfa.broker.mock import MockBroker

    shared = MockBroker({"cash": 500.0, "prices": {"513050": 1.092}})  # too poor
    shared.connect()
    monkeypatch.setattr(climod, "get_adapter", lambda cfg: shared)

    code, out = run_cli("--config", str(p), "--broker", "mock",
                        "buy", "513050", "--qty", "2800", "--force", capsys=capsys)
    assert out["ok"] is False
    assert out["error"] == "broker_error"
    assert "insufficient cash" in out["message"]


def test_dry_run_reports_decision_even_outside_hours(capsys, tmp_path, monkeypatch):
    """A dry run submits nothing, so it must still explain the intent when the
    market is closed — otherwise it cannot be used to validate parameters."""
    cfg = write_config(tmp_path)
    import etfa.cli as climod
    from etfa.broker.mock import MockBroker

    shared = MockBroker({"cash": 100000})
    shared.connect()
    monkeypatch.setattr(climod, "get_adapter", lambda cfg: shared)

    code, out = run_cli("--config", str(cfg), "--broker", "mock",
                        "buy", "513050", "--qty", "100", "--dry-run", capsys=capsys)
    assert out["ok"] is True
    assert out["submitted"] is False
    assert out["decision"]["qty"] == 100
    assert shared.calls == []          # nothing reached the broker


def test_market_invocation_prints_one_json_object(tmp_path):
    """The documented entry point `python -m etfa` must work from a shell."""
    cfg = write_config(tmp_path)
    env = {**__import__("os").environ, "PYTHONPATH": str(REPO / "src")}
    proc = subprocess.run(
        [sys.executable, "-m", "etfa", "--config", str(cfg), "session"],
        capture_output=True, text=True, env=env, cwd=str(REPO),
    )
    assert proc.returncode == 0, proc.stderr
    obj = json.loads(proc.stdout.strip())
    assert obj["ok"] is True
    assert "session" in obj
