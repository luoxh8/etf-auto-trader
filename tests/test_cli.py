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


def test_doctor_checks_the_automation_stack_for_a_live_broker(capsys, tmp_path):
    """The live path needs pywin32/comtypes/pywinauto. That is independent of
    any client, so doctor must verify it and report it as blocking."""
    cfg = {
        "broker": {"type": "easytrader_ths", "clienttype": "ths", "exe_path": "C:\\x\\xiadan.exe"},
        "trading": {"allowed_codes": ["513050"]},
        "paths": {"var": str(tmp_path / "var")},
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")

    code, out = run_cli("--config", str(p), "doctor", capsys=capsys)
    assert out["ok"] is True
    names = {c["check"]: c for c in out["checks"]}
    assert "automation_stack" in names
    # on this machine the stack is verified working; if it ever breaks, doctor
    # must say so rather than letting a live run fail obscurely
    assert names["automation_stack"]["blocking"] is True
    assert names["automation_stack"]["detail"]


def test_doctor_rejects_a_nonexistent_exe_path(capsys, tmp_path):
    """A typo in exe_path is the likeliest first-run problem, so doctor must
    not report OK just because the string is non-empty."""
    cfg = {
        "broker": {"type": "easytrader_ths", "clienttype": "ths",
                   "exe_path": r"C:\nope\does-not-exist\xiadan.exe"},
        "trading": {"allowed_codes": ["513050"]},
        "paths": {"var": str(tmp_path / "var")},
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")

    code, out = run_cli("--config", str(p), "doctor", capsys=capsys)
    names = {c["check"]: c for c in out["checks"]}
    assert names["exe_path"]["ok"] is False
    assert "does not exist" in names["exe_path"]["detail"]
    assert out["ready"] is False          # it is a blocking check


def test_doctor_accepts_an_existing_exe_path(capsys, tmp_path):
    fake_exe = tmp_path / "xiadan.exe"
    fake_exe.write_bytes(b"not really an exe")
    cfg = {
        "broker": {"type": "easytrader_ths", "clienttype": "ths", "exe_path": str(fake_exe)},
        "trading": {"allowed_codes": ["513050"]},
        "paths": {"var": str(tmp_path / "var")},
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")

    code, out = run_cli("--config", str(p), "doctor", capsys=capsys)
    names = {c["check"]: c for c in out["checks"]}
    assert names["exe_path"]["ok"] is True


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


def _run_with_config(capsys, tmp_path, cfg: dict, argv: list, monkeypatch=None, broker=None):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return run_cli(*(["--config", str(p)] + argv), capsys=capsys)


def test_rejected_order_does_not_latch_strategy_state(capsys, tmp_path, monkeypatch):
    """A refused order must not leave the grid believing it owns a lot.

    Regression: plan() latches the level optimistically; if the guard then
    refuses the order and the state is persisted anyway, the grid later tries to
    SELL shares it never bought and wedges permanently. Verified against the
    real CLI before the fix (buy_state was [1,0,0,0] with an empty account).
    """
    import etfa.cli as climod
    from etfa.broker.mock import MockBroker

    cfg = {
        "broker": {"type": "mock"},
        "trading": {"allowed_codes": ["513050"], "max_order_amount": 1000.0,
                    "max_daily_amount": 0.0, "min_order_amount": 0.0,
                    "max_position_pct": 100.0},
        "paths": {"var": str(tmp_path / "var")},
    }
    # price below the first buy level so the grid wants to buy
    shared = MockBroker({"cash": 50000.0, "prices": {"513050": 0.96}, "positions": []})
    shared.connect()
    monkeypatch.setattr(climod, "get_adapter", lambda c: shared)

    code, out = _run_with_config(
        capsys, tmp_path, cfg,
        ["--broker", "mock", "run", "grid", "--code", "513050",
         "--base", "1.0", "--spacing", "3", "--qty", "2800"],
        monkeypatch,
    )
    assert out["rejected"] == 1
    assert out["placed"] == 0
    assert out["rolled_back"] is True
    # the latch must be back to idle
    assert out["state"]["buy_state"] == [0, 0, 0, 0]
    # and it must be what was persisted, not just what we reported
    sf = tmp_path / "var" / "grid-513050.json"
    saved = json.loads(sf.read_text(encoding="utf-8"))
    assert saved["buy_state"] == [0, 0, 0, 0]


def test_accepted_order_does_latch_strategy_state(capsys, tmp_path, monkeypatch):
    """The complement: a filled order must commit the latch, so the grid will
    later take profit on it. Without this the rollback would be a stop-the-world."""
    import etfa.cli as climod
    from etfa.broker.mock import MockBroker

    cfg = {
        "broker": {"type": "mock"},
        "trading": {"allowed_codes": ["513050"], "max_order_amount": 0.0,
                    "max_daily_amount": 0.0, "min_order_amount": 0.0,
                    "max_position_pct": 100.0},
        "paths": {"var": str(tmp_path / "var")},
    }
    shared = MockBroker({"cash": 50000.0, "prices": {"513050": 0.96}, "positions": []})
    shared.connect()
    monkeypatch.setattr(climod, "get_adapter", lambda c: shared)

    code, out = _run_with_config(
        capsys, tmp_path, cfg,
        ["--broker", "mock", "run", "grid", "--code", "513050",
         "--base", "1.0", "--spacing", "3", "--qty", "2800", "--force"],
        monkeypatch,
    )
    assert out["placed"] == 1
    assert out["rolled_back"] is False
    assert out["state"]["buy_state"] == [1, 0, 0, 0]   # level latched LONG
    assert shared.position_map()["513050"].qty == 2800


def test_plan_still_discards_state(capsys, tmp_path, monkeypatch):
    """`plan` must not persist anything — its latches are hypothetical."""
    import etfa.cli as climod
    from etfa.broker.mock import MockBroker

    cfg = {
        "broker": {"type": "mock"},
        "trading": {"allowed_codes": ["513050"], "max_order_amount": 0.0,
                    "min_order_amount": 0.0, "max_daily_amount": 0.0,
                    "max_position_pct": 100.0},
        "paths": {"var": str(tmp_path / "var")},
    }
    shared = MockBroker({"cash": 50000.0, "prices": {"513050": 0.96}, "positions": []})
    shared.connect()
    monkeypatch.setattr(climod, "get_adapter", lambda c: shared)

    code, out = _run_with_config(
        capsys, tmp_path, cfg,
        ["--broker", "mock", "plan", "grid", "--code", "513050",
         "--base", "1.0", "--spacing", "3", "--qty", "2800"],
        monkeypatch,
    )
    assert out["intents"], "plan should describe the intent"
    assert shared.calls == []      # nothing reached the broker
    assert not (tmp_path / "var" / "grid-513050.json").exists()


def test_plan_dry_run_reports_decision_without_persisting(capsys, tmp_path, monkeypatch):
    """plan describes the intent and never touches the broker or the state file."""
    import etfa.cli as climod
    from etfa.broker.mock import MockBroker

    cfg = write_config(tmp_path)
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
