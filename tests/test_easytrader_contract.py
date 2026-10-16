"""Contract tests against the REAL easytrader classes.

Every other live-path test uses a hand-written fake. A fake can drift from the
library it imitates, which would let a real API change slip through. These tests
import the actual installed easytrader and assert the assumptions this project
makes about it — so a version bump that changes any of them fails loudly here
instead of at the user's first live order.

No client and no network are needed: `easytrader.use()` only builds an object.
"""
from __future__ import annotations

import builtins
import inspect

import pytest

easytrader = pytest.importorskip("easytrader")

from etfa.broker.easytrader_ths import (  # noqa: E402
    BALANCE_FIELDS,
    ENTRUST_FIELDS,
    POSITION_FIELDS,
    _match_fields,
)


# ---- the API shape we depend on -----------------------------------------

def test_use_ths_returns_a_clienttrader():
    from easytrader.clienttrader import ClientTrader

    trader = easytrader.use("ths")
    assert isinstance(trader, ClientTrader)


@pytest.mark.parametrize("name", ["balance", "position", "today_entrusts",
                                  "today_trades", "cancel_entrusts"])
def test_read_capabilities_are_properties_not_methods(name):
    """If easytrader ever turns these into methods, `_attr` still works — but
    this project's `_call` usage would be wrong, so we pin the fact."""
    from easytrader.clienttrader import ClientTrader

    attr = inspect.getattr_static(ClientTrader, name)
    assert isinstance(attr, property), (
        f"{name} is {type(attr).__name__}, not a property — "
        "re-check adapter/easytrader_ths.py before shipping"
    )


def test_write_capabilities_are_callables_with_the_expected_signature():
    from easytrader.clienttrader import ClientTrader

    for name in ("buy", "sell", "cancel_entrust", "cancel_all_entrusts",
                 "connect", "refresh", "wait", "exit"):
        assert callable(getattr(ClientTrader, name)), f"{name} must be callable"

    # buy/sell: (self, security, price, amount, **kwargs) — three positionals
    for name in ("buy", "sell"):
        params = list(inspect.signature(getattr(ClientTrader, name)).parameters)
        assert params[:4] == ["self", "security", "price", "amount"], (
            f"{name} signature changed: {params}"
        )


def test_universal_client_trader_has_prepare_for_the_fallback_path():
    """docs/COMPATIBILITY.md offers universal_client as the fallback; make sure
    the methods the adapter calls there really exist."""
    from easytrader.universal_clienttrader import UniversalClientTrader

    for name in ("prepare", "connect", "buy", "sell"):
        assert hasattr(UniversalClientTrader, name), f"missing {name}"
    params = list(inspect.signature(UniversalClientTrader.prepare).parameters)
    assert "user" in params and "password" in params and "exe_path" in params


# ---- the field mapping must cover what easytrader itself defines ---------

def test_balance_fields_cover_the_real_ths_config_keys():
    """This is the substantive one: easytrader's own `ths` config declares which
    balance keys the client produces. Our alias table must cover all of them, or
    a real balance read silently loses a field."""
    from easytrader.config import client as client_mod

    cfg = client_mod.create("ths")
    real_keys = list(getattr(cfg, "BALANCE_CONTROL_ID_GROUP", {}).keys())
    assert real_keys, "easytrader ths config no longer exposes balance keys"

    result = _match_fields(real_keys, BALANCE_FIELDS)
    covered = set(result["found"].values())
    uncovered = [k for k in real_keys if k not in covered]
    assert not uncovered, (
        f"these real easytrader balance keys are unmapped by BALANCE_FIELDS: {uncovered}"
    )
    # and everything the client actually declares must satisfy our required set
    assert result["missing"] == [], (
        f"required balance fields not provided by the real ths config: {result['missing']}"
    )


def test_entrust_fields_cover_the_cancel_field_easytrader_matches_on():
    """easytrader's cancel_entrust() looks orders up by the column named in its
    config's CANCEL_ENTRUST_ENTRUST_FIELD. If our alias table does not cover that
    column, the id we expose can never match and cancelling silently fails."""
    from easytrader.config import client as client_mod

    cfg = client_mod.create("ths")
    cancel_field = getattr(cfg, "CANCEL_ENTRUST_ENTRUST_FIELD", None)
    assert cancel_field, "easytrader ths config no longer declares a cancel field"

    # simulate a row carrying that column
    res = _match_fields([cancel_field], ENTRUST_FIELDS)
    assert res["found"], (
        f"the cancel field {cancel_field!r} is not covered by ENTRUST_FIELDS — "
        "cancel would never match a real order"
    )


def test_entrust_fields_cover_the_grid_dtype_columns():
    """GRID_DTYPE lists the columns easytrader expects the client grid to have.
    Overlap with our aliases tells us the mapping is grounded in reality."""
    from easytrader.config import client as client_mod

    cfg = client_mod.create("ths")
    grid_cols = list(getattr(cfg, "GRID_DTYPE", {}).keys())
    if not grid_cols:
        pytest.skip("no GRID_DTYPE in this easytrader version")
    res = _match_fields(grid_cols, ENTRUST_FIELDS)
    # the code column must always be identifiable
    assert "code" in res["found"], f"none of GRID_DTYPE matched our code aliases: {grid_cols}"


def test_no_module_in_the_live_path_imports_an_undeclared_dependency():
    """The window-title guard silently returned None for its whole life because
    it imported psutil, which the project never declared. Any module-level or
    lazy import in the live path must resolve in a bare environment.

    This walks the AST of every module in the package and flags imports that are
    neither stdlib, nor the package itself, nor declared in pyproject.
    """
    import ast
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    src = repo / "src" / "etfa"

    declared = {"easytrader", "pywin32", "pytest", "setuptools"}
    # map distribution name -> import name where they differ
    import_aliases = {"pywin32": "win32api", "win32api": "win32api"}
    local_ok = {"win32api", "win32gui", "win32process", "win32con", "comtypes",
                "pywinauto", "easytrader", "etfa"}

    stdlib = set(sys.stdlib_module_names)
    problems = []
    for py in src.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:          # relative import, always fine
                    continue
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                if not name:
                    continue
                if name in stdlib or name in local_ok or name.startswith("etfa"):
                    continue
                problems.append(f"{py.relative_to(repo)}: {name}")

    assert not problems, (
        "these imports are not stdlib, local, or a declared dependency — they "
        f"would fail at runtime in a clean install: {problems}"
    )


def test_window_detection_uses_only_pywin32(monkeypatch):
    """Guard the specific regression: detection must not need psutil."""
    import importlib
    import sys as _sys

    import etfa.broker.easytrader_ths as mod

    # make `import psutil` fail if anything tries it
    real_import = builtins.__import__

    def deny_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ModuleNotFoundError("psutil is not installed (simulated)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_psutil)
    # must not raise; may legitimately return None on a machine with no such process
    mod.detect_client_window_title(r"C:\no\such\program.exe")


def test_every_logical_field_has_at_least_one_plausible_alias():
    for fields in (BALANCE_FIELDS, POSITION_FIELDS, ENTRUST_FIELDS):
        for spec in fields:
            # (field, aliases, required)
            assert len(spec) == 3, f"field spec must be (name, aliases, required): {spec}"
            field, aliases, required = spec
            assert aliases, f"{field} has no aliases"
            assert all(isinstance(a, str) and a for a in aliases)
            assert isinstance(required, bool)


def test_required_fields_are_a_subset_of_all_aliases():
    """Nothing optional may secretly be load-bearing."""
    for fields in (BALANCE_FIELDS, POSITION_FIELDS):
        required = {f for f, _a, r in fields if r}
        assert required, "at least one field must be required"


def test_match_fields_separates_required_from_optional_misses():
    """A config that omits 冻结金额 (generic ths does) must not look broken."""
    res = _match_fields(["资金余额", "可用金额", "总资产"], BALANCE_FIELDS)
    assert res["missing"] == []                       # all required present
    assert "frozen" in res["missing_optional"]        # absent, but optional
    assert "withdrawable" in res["missing_optional"]


def test_match_fields_reports_both_found_and_missing():
    res = _match_fields(["证券代码", "证券名称"], POSITION_FIELDS)
    assert res["found"]["code"] == "证券代码"
    assert res["found"]["name"] == "证券名称"
    assert "qty" in res["missing"] and "last" in res["missing_optional"]
    assert "aliases" in res
