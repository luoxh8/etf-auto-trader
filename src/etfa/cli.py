"""The `etfa` command surface.

Design constraints, in order of importance:

1. **Machine-first.** Every command prints exactly one JSON object to stdout
   and nothing else, so an AI (or a shell pipeline) can parse it without
   scraping human text. Errors are JSON too, with a stable ``error`` code.
2. **Safe by default.** ``plan`` never orders. ``run`` and the primitive
   ``buy``/``sell`` always pass through the risk guard. Nothing trades outside
   trading hours unless ``--force`` is given.
3. **No secrets on disk.** The live adapter attaches to a client the user has
   already logged into; there is no password argument.

Run ``etfa --help`` or ``etfa <command> --help`` for the full surface.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List, Optional

from . import __version__
from .audit import AuditEvent, AuditLog
from .broker import get_adapter
from .config import Config, load_config
from .errors import EtfaError, RiskRejected, TradingHoursError
from .models import OrderRequest, Quote, SIDE_BUY, SIDE_SELL, now_iso
from .risk import RiskGuard
from .strategies import build as build_strategy
from .strategies.base import StrategyContext
from .timeutil import classify, next_open_after, session_note


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------

def emit(obj: Dict[str, Any], exit_code: int = 0) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
    raise SystemExit(exit_code)


def ok(**fields: Any) -> None:
    emit({"ok": True, **fields})


def fail(code: str, message: str, exit_code: int = 2, **extra: Any) -> None:
    emit({"ok": False, "error": code, "message": message, **extra}, exit_code=exit_code)


# --------------------------------------------------------------------------
# context construction
# --------------------------------------------------------------------------

class App:
    """Holds the loaded config, audit log, adapter and guard for one command."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        try:
            self.cfg: Config = load_config(getattr(args, "config", None))
        except EtfaError as exc:
            fail(exc.code, str(exc))
        if getattr(args, "broker", None):
            self.cfg.broker.type = args.broker
        self.audit = AuditLog(self.cfg.audit_path())
        self.guard = RiskGuard(self.cfg, self.audit)
        self._adapter = None

    @property
    def adapter(self):
        if self._adapter is None:
            self._adapter = get_adapter(self.cfg)
        return self._adapter

    def connect(self) -> None:
        a = self.adapter
        if not a.is_connected():
            a.connect()

    def note(self, kind: str, **kw: Any) -> None:
        self.audit.append(AuditEvent(ts=now_iso(), kind=kind, **kw))

    def context(self, codes: List[str], price_override: Optional[float] = None) -> StrategyContext:
        quotes = {}
        try:
            quotes = self.adapter.quote(codes)
        except EtfaError:
            quotes = {}
        # easytrader has no quote feed, so a code that is not held comes back
        # with no price. An explicit --price lets strategies run anyway.
        if price_override and price_override > 0:
            for c in codes:
                if c not in quotes:
                    quotes[c] = Quote(code=c, last=round(price_override, 3),
                                      bid1=round(price_override - 0.001, 3),
                                      ask1=round(price_override + 0.001, 3),
                                      prev_close=round(price_override, 3), ts=now_iso())
        try:
            positions = self.adapter.position_map()
        except EtfaError:
            positions = {}
        try:
            bal = self.adapter.balance()
            cash = bal.available
        except EtfaError:
            cash = 0.0
        return StrategyContext(
            quotes=quotes,
            positions=positions,
            cash_available=cash,
            session_label=classify(holidays_file=self.cfg.trading.holidays_file or None).label,
        )

    def guard_and_submit(self, req: OrderRequest, force: bool, submit: bool,
                         enforce_hours: bool = True) -> Dict[str, Any]:
        ctx = self.context([req.code])
        q = ctx.quote(req.code)
        try:
            bal = self.adapter.balance()
            total_assets = bal.total
        except EtfaError:
            total_assets = 0.0
        try:
            decision = self.guard.evaluate(
                req, q, force=force, broker=self.adapter.name,
                position_qty=ctx.held(req.code), sellable=ctx.sellable(req.code),
                total_assets=total_assets, enforce_hours=enforce_hours,
            )
        except RiskRejected as exc:
            self.guard.reject(req, exc, code_label="risk_rejected")
            return {"ok": False, "error": exc.code, "message": str(exc), "intent": _req_dict(req)}
        except TradingHoursError as exc:
            self.guard.reject(req, exc, code_label="outside_trading_hours")
            return {"ok": False, "error": exc.code, "message": str(exc), "intent": _req_dict(req)}

        self.audit.append(AuditEvent(
            ts=now_iso(), kind="intent", code=req.code, side=req.side,
            qty=decision.qty, price=decision.price, amount=decision.amount,
            detail=req.reason, broker=self.adapter.name,
        ))

        if not submit:
            return {"ok": True, "submitted": False, "decision": decision.to_dict(),
                    "intent": _req_dict(req)}

        # submit
        try:
            if req.side == SIDE_BUY:
                order = self.adapter.buy(req.code, decision.price, decision.qty)
            else:
                order = self.adapter.sell(req.code, decision.price, decision.qty)
        except EtfaError as exc:
            self.audit.append(AuditEvent(ts=now_iso(), kind="error", ok=False, code=req.code,
                                         side=req.side, detail=str(exc), broker=self.adapter.name))
            return {"ok": False, "error": exc.code, "message": str(exc), "decision": decision.to_dict()}

        placed_ok = order.status != "rejected"
        self.audit.append(AuditEvent(
            ts=now_iso(), kind="placed", ok=placed_ok, code=req.code,
            side=req.side, qty=decision.qty, price=decision.price, amount=decision.amount,
            order_id=order.id, broker=self.adapter.name,
            detail=order.message or req.reason,
        ))
        if not placed_ok:
            # the broker refused it — report that, do not pretend it worked
            return {"ok": False, "error": "broker_error", "submitted": False,
                    "message": order.message or "broker rejected the order",
                    "decision": decision.to_dict(), "order": order.to_dict()}
        return {"ok": True, "submitted": True, "decision": decision.to_dict(), "order": order.to_dict()}


def _req_dict(req: OrderRequest) -> Dict[str, Any]:
    return {"code": req.code, "side": req.side, "qty": req.qty, "amount": req.amount,
            "price": req.price, "reason": req.reason}


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_status(app: App, args) -> None:
    s = classify(holidays_file=app.cfg.trading.holidays_file or None)
    out: Dict[str, Any] = {
        "version": __version__,
        "config_root": str(app.cfg.root),
        "broker_type": app.cfg.broker.type,
        "session": s.to_dict(),
        "session_note": session_note(app.cfg.trading.holidays_file or None),
        "now": now_iso(),
        "next_open": next_open_after().isoformat(timespec="minutes"),
        "allowed_codes": app.cfg.trading.allowed_codes,
        "t0_codes": app.cfg.trading.t0_codes,
    }
    connected = False
    if args.connect:
        try:
            app.connect()
            connected = True
        except EtfaError as exc:
            out["connect_error"] = {"error": exc.code, "message": str(exc)}
    out["connected"] = connected
    if connected:
        try:
            b = app.adapter.balance()
            out["balance"] = {"cash": b.cash, "available": b.available,
                              "market_value": b.market_value, "total": b.total}
        except EtfaError as exc:
            out["balance_error"] = {"error": exc.code, "message": str(exc)}
    ok(**out)


def cmd_session(app: App, args) -> None:
    s = classify(holidays_file=app.cfg.trading.holidays_file or None)
    ok(session=s.to_dict(), note=session_note(app.cfg.trading.holidays_file or None), now=now_iso())


def cmd_balance(app: App, args) -> None:
    app.connect()
    b = app.adapter.balance()
    ok(balance=b.__dict__)


def cmd_positions(app: App, args) -> None:
    app.connect()
    ps = app.adapter.positions()
    ok(positions=[p.__dict__ | {"market_value": p.market_value} for p in ps])


def cmd_quote(app: App, args) -> None:
    app.connect()
    codes = args.codes or app.cfg.trading.allowed_codes
    if not codes:
        fail("config_error", "no codes given and trading.allowed_codes is empty")
    q = app.adapter.quote(codes)
    ok(quotes={k: v.to_dict() for k, v in q.items()},
       missing=[c for c in codes if c not in q])


def cmd_orders(app: App, args) -> None:
    app.connect()
    os_ = app.adapter.orders(open_only=args.open)
    ok(orders=[o.to_dict() for o in os_], count=len(os_))


def cmd_cancel(app: App, args) -> None:
    app.connect()
    ids = app.adapter.cancel(args.order_id)
    for i in ids:
        app.note("cancel", order_id=i, broker=app.adapter.name)
    ok(cancelled=ids, count=len(ids))


def _make_req(args) -> OrderRequest:
    return OrderRequest(
        code=args.code, side=args.side, qty=getattr(args, "qty", None),
        amount=getattr(args, "amount", None), price=getattr(args, "price", 0.0) or 0.0,
        reason=getattr(args, "reason", "") or "manual",
    )


def cmd_buy(app: App, args) -> None:
    args.side = SIDE_BUY
    app.connect()
    # a dry run submits nothing, so it reports the decision even outside hours
    res = app.guard_and_submit(_make_req(args), force=args.force,
                               submit=not args.dry_run, enforce_hours=not args.dry_run)
    emit(res, exit_code=0 if res.get("ok") else 2)


def cmd_sell(app: App, args) -> None:
    args.side = SIDE_SELL
    app.connect()
    res = app.guard_and_submit(_make_req(args), force=args.force,
                               submit=not args.dry_run, enforce_hours=not args.dry_run)
    emit(res, exit_code=0 if res.get("ok") else 2)


def _strategy_from_args(args):
    name = args.strategy
    if name == "grid":
        return build_strategy(
            "grid",
            code=args.code, base_price=args.base, spacing_pct=args.spacing,
            levels_up=args.levels_up, levels_down=args.levels_down,
            qty_per_level=args.qty, lower_bound=args.lower, upper_bound=args.upper,
        )
    if name == "t":
        return build_strategy(
            "t", code=args.code, qty=args.qty, trigger_pct=args.trigger,
            take_profit_pct=args.take_profit, t0=args.t0,
        )
    fail("config_error", f"unknown strategy {name!r}")


def _state_file(app: App, name: str, code: str) -> str:
    return str(app.cfg.state_path(f"{name}-{code}.json"))


def cmd_plan(app: App, args) -> None:
    """Dry-run: show what the strategy wants to do, order nothing."""
    app.connect()
    strat = _strategy_from_args(args)
    sf = _state_file(app, strat.name, args.code)
    _load_state(strat, sf)
    ctx = app.context([args.code], price_override=args.price)
    reqs = strat.plan(ctx)
    decisions = []
    for r in reqs:
        # planning is read-only: report the intent even outside trading hours,
        # but still run every other guardrail so the plan is honest about limits
        d = app.guard_and_submit(r, force=args.force, submit=False, enforce_hours=False)
        decisions.append(d)
    ok(strategy=strat.name, code=args.code, session=ctx.session_label,
       quote=(ctx.quote(args.code).to_dict() if ctx.quote(args.code) else None),
       intents=[_req_dict(r) for r in reqs],
       decisions=decisions, state=strat.dump_state())


def cmd_run(app: App, args) -> None:
    """One strategy tick: plan, guard, submit for real."""
    app.connect()
    strat = _strategy_from_args(args)
    sf = _state_file(app, strat.name, args.code)
    _load_state(strat, sf)
    ctx = app.context([args.code], price_override=args.price)
    reqs = strat.plan(ctx)
    results = []
    for r in reqs:
        results.append(app.guard_and_submit(r, force=args.force, submit=True))
    _save_state(strat, sf)
    ok(strategy=strat.name, code=args.code, session=ctx.session_label,
       placed=sum(1 for x in results if x.get("submitted")),
       rejected=sum(1 for x in results if not x.get("ok")),
       results=results, state=strat.dump_state())


def cmd_watch(app: App, args) -> None:
    """Loop the strategy. Bounded by --iterations so an AI cannot leak a daemon."""
    app.connect()
    strat = _strategy_from_args(args)
    sf = _state_file(app, strat.name, args.code)
    _load_state(strat, sf)
    iterations = args.iterations
    placed_total = 0
    ticks = []
    for i in range(iterations):
        ctx = app.context([args.code], price_override=args.price)
        reqs = strat.plan(ctx)
        row = {"tick": i + 1, "ts": now_iso(), "session": ctx.session_label,
               "price": (ctx.quote(args.code).last if ctx.quote(args.code) else None),
               "intents": len(reqs), "results": []}
        for r in reqs:
            res = app.guard_and_submit(r, force=args.force, submit=not args.dry_run)
            row["results"].append(res)
            if res.get("submitted"):
                placed_total += 1
        ticks.append(row)
        _save_state(strat, sf)
        if i + 1 < iterations:
            time.sleep(args.interval)
    ok(strategy=strat.name, code=args.code, iterations=iterations,
       placed=placed_total, ticks=ticks, state=strat.dump_state())


def cmd_grid_ladder(app: App, args) -> None:
    strat = build_strategy("grid", code=args.code, base_price=args.base,
                           spacing_pct=args.spacing, levels_up=args.levels_up,
                           levels_down=args.levels_down, qty_per_level=args.qty)
    ok(ladder=strat.ladder(), state=strat.dump_state())


def _automation_stack() -> Dict[str, Any]:
    """Check the layers easytrader needs to drive a desktop client.

    pywinauto -> comtypes -> pywin32, plus the clipboard helper easytrader uses
    to read table data. All of this is independent of the brokerage client, so it
    can be verified on any machine, with or without a client installed.
    """
    problems = []
    for mod, label in (("win32api", "pywin32"), ("comtypes", "comtypes"),
                       ("pywinauto", "pywinauto")):
        try:
            __import__(mod)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{label}: {type(exc).__name__}")
    visible = None
    if not problems:
        try:
            from pywinauto import Application, Desktop, findwindows, timings  # noqa: F401
            import pywinauto.clipboard  # noqa: F401
            visible = len([w for w in Desktop(backend="win32").windows() if w.is_visible()])
        except Exception as exc:  # noqa: BLE001
            problems.append(f"enumerate windows: {type(exc).__name__}: {exc}")
    if problems:
        return {"ok": False, "detail": "; ".join(problems)}
    return {"ok": True, "detail": f"pywin32/comtypes/pywinauto ok; {visible} visible windows"}


def cmd_probe(app: App, args) -> None:
    """Read-only diagnostic for the live client: what can we actually see?

    Run this the first time the client is connected. It shows the raw payloads
    and which of the expected Chinese columns matched, which is where a
    client-specific mismatch would show up.
    """
    app.connect()
    probe = getattr(app.adapter, "probe", None)
    if probe is None:
        fail("unsupported_feature",
             f"broker '{app.adapter.name}' has no probe; it is a live-client diagnostic")
    raw = probe()
    # a compact verdict an AI can branch on
    verdict: Dict[str, Any] = {}
    win = raw.get("window") or {}
    verdict["window_title_ok"] = win.get("title_match")
    for key in ("balance", "position", "today_entrusts"):
        section = raw.get(key) or {}
        mapping = section.get("mapping") or {}
        rows = section.get("row_count", 0)
        # An EMPTY result is not a mismatch: with no rows there are no column
        # names to compare, so we cannot judge. Only a non-empty result whose
        # REQUIRED columns do not map is a real incompatibility (optional columns
        # are legitimately absent in some client configs).
        verdict[key] = {
            "readable": "error" not in section,
            "rows": rows,
            "fields_missing": mapping.get("missing", []) if rows else [],
            "fields_missing_optional": mapping.get("missing_optional", []) if rows else [],
            "indeterminate": rows == 0 and "error" not in section,
        }
    # one overall answer: readable everywhere, no mapped-field gaps, title fine
    readings_ok = all(
        verdict[k]["readable"] and not verdict[k]["fields_missing"]
        for k in ("balance", "position", "today_entrusts")
    )
    verdict["compatible"] = readings_ok and verdict.get("window_title_ok") in (True, None)
    ok(broker=app.adapter.name, verdict=verdict, raw=raw)


def cmd_simulate(app: App, args) -> None:
    """Run a strategy over a synthetic price path, offline.

    Answers "does this survive a session, net of the ¥5 minimum commission?"
    without touching a broker. The strategy and risk guard are the shipped ones.
    """
    from .simulate import (
        CostModel,
        compare_cost_regimes,
        simulate_grid,
        simulate_t,
    )

    cost = CostModel(
        commission_rate=args.commission_rate,
        min_commission=0.0 if args.no_min_commission else args.min_commission,
    )
    common = dict(
        code=args.code,
        base_price=args.base,
        starting_cash=args.cash,
        ticks=args.ticks,
        seed=args.seed,
    )

    if args.strategy == "grid":
        runner = lambda sc: simulate_grid(  # noqa: E731
            spacing_pct=args.spacing, qty_per_level=args.qty,
            levels_up=args.levels_up, levels_down=args.levels_down,
            starting_position=args.position or int(args.cash * 0.4 / args.base // 100 * 100),
            scenario=sc, cost=cost, **common,
        )
    else:  # the intraday-T strategy
        runner = lambda sc: simulate_t(  # noqa: E731
            qty=args.qty, trigger_pct=args.trigger, take_profit_pct=args.take_profit,
            t0=args.t0, scenario=sc, cost=cost, **common,
        )

    if args.compare_fees:
        # compare_cost_regimes owns its two CostModels
        if args.strategy != "grid":
            fail("unsupported_feature", "--compare-fees currently supports --strategy grid only")
        out = compare_cost_regimes(
            commission_rate=args.commission_rate, min_commission=args.min_commission,
            code=args.code, base_price=args.base, spacing_pct=args.spacing,
            qty_per_level=args.qty, levels_up=args.levels_up, levels_down=args.levels_down,
            starting_cash=args.cash,
            starting_position=args.position or int(args.cash * 0.4 / args.base // 100 * 100),
            ticks=args.ticks, seed=args.seed,
        )
        ok(**out)
        return

    if args.scenario == "all":
        results = {sc: runner(sc).to_dict() for sc in ("range", "up", "down")}
        ok(strategy=args.strategy, scenarios=results, costs=cost.describe(),
           verdict=_sim_verdict(results))
        return

    res = runner(args.scenario)
    ok(strategy=args.strategy, **res.to_dict(), costs=cost.describe())


def _sim_verdict(results: Dict[str, Any]) -> Dict[str, Any]:
    """A blunt read of the three-scenario sweep, so an AI need not interpret."""
    rng = results.get("range", {})
    return {
        "range_net_pnl": rng.get("net_pnl"),
        "up_net_pnl": results.get("up", {}).get("net_pnl"),
        "down_net_pnl": results.get("down", {}).get("net_pnl"),
        "profitable_in_range": (rng.get("net_pnl") or 0) > 0,
        "worst_case": min((v.get("net_pnl") or 0) for v in results.values()),
        "reminder": (
            "Both strategies earn when price oscillates and lose when it trends. "
            "Read 'down' as 'what would I lose if it just kept falling' — that is "
            "the risk you accept by running this. Compare 'net_pnl' against "
            "fees_paid: if fees are a large share of gross, renegotiate the ¥5 "
            "minimum before tuning anything else."
        ),
    }


def cmd_audit(app: App, args) -> None:
    events = app.audit.tail(args.tail)
    ok(events=events, count=len(events), notional_today=app.audit.notional_today())


def cmd_config_show(app: App, args) -> None:
    ok(config=app.cfg.to_public_dict())


def cmd_doctor(app: App, args) -> None:
    """Pre-flight: what is ready and what is missing, without trading.

    ``ready`` means "the tool is configured well enough to trade"; whether the
    market is *open* right now is reported separately as ``market_open`` — being
    closed for the day must not look like a broken install.
    """
    checks: List[Dict[str, Any]] = []

    def check(name: str, okflag: bool, detail: str, blocking: bool = True) -> None:
        checks.append({"check": name, "ok": bool(okflag), "detail": detail, "blocking": blocking})

    check("config", True, str(app.cfg.root))
    check("allowed_codes", bool(app.cfg.trading.allowed_codes),
          f"{app.cfg.trading.allowed_codes}" if app.cfg.trading.allowed_codes else "empty (all codes blocked)")
    if app.cfg.broker.type == "easytrader_ths":
        try:
            import easytrader  # noqa: F401
            check("easytrader_installed", True, "import ok")
        except ImportError:
            check("easytrader_installed", False,
                  "install the [live] extra with: pip install -e \".[live]\" -i https://pypi.org/simple")
        # Can this machine drive desktop windows at all? This is independent of
        # the brokerage client, so it can be (and is) verified without one.
        stack = _automation_stack()
        check("automation_stack", stack["ok"], stack["detail"], blocking=True)
        # the exe path must actually exist — a typo here is the most likely
        # first-run problem, so do not report OK just because the string is set
        exe = app.cfg.broker.exe_path
        from pathlib import Path as _Path

        if not exe:
            check("exe_path", False, "broker.exe_path is empty (required for clienttype='ths')")
        elif not _Path(exe).exists():
            check("exe_path", False,
                  f"file does not exist: {exe} — run scripts\\find-client.ps1 to locate it")
        else:
            check("exe_path", True, exe)

    s = classify(holidays_file=app.cfg.trading.holidays_file or None)
    check("market_open", s.tradable, f"{s.label} (tradable={s.tradable})", blocking=False)

    blocking_ok = all(c["ok"] for c in checks if c["blocking"])
    ok(checks=checks, ready=blocking_ok, market_open=s.tradable,
       can_trade_now=(blocking_ok and s.tradable))


def _load_state(strat, path: str) -> None:
    from pathlib import Path
    p = Path(path)
    if p.exists():
        try:
            strat.load_state(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass


def _save_state(strat, path: str) -> None:
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(strat.dump_state(), ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# argument parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="etfa", description="CLI-driven ETF auto-trading.")
    p.add_argument("--config", help="path to config.json (default: ./config/config.json)")
    p.add_argument("--broker", help="override broker.type: mock | easytrader_ths | qmt")
    p.add_argument("--version", action="version", version=f"etfa {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("status", help="config + session + optional live balance")
    sp.add_argument("--connect", action="store_true", help="also try to connect to the broker")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("session", help="trading-session classification")
    sp.set_defaults(func=cmd_session)

    sp = sub.add_parser("doctor", help="pre-flight checks, no trading")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("probe", help="live-client diagnostic: what can we read? (read-only)")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("config", help="show effective config")
    sp.set_defaults(func=cmd_config_show)

    sp = sub.add_parser("balance", help="account balance")
    sp.set_defaults(func=cmd_balance)

    sp = sub.add_parser("positions", help="current positions")
    sp.set_defaults(func=cmd_positions)

    sp = sub.add_parser("quote", help="quotes for codes")
    sp.add_argument("codes", nargs="*", help="6-digit codes (default: allowed_codes)")
    sp.set_defaults(func=cmd_quote)

    sp = sub.add_parser("orders", help="today's orders")
    sp.add_argument("--open", action="store_true", help="only open orders")
    sp.set_defaults(func=cmd_orders)

    sp = sub.add_parser("cancel", help="cancel one order, or all open orders")
    sp.add_argument("order_id", nargs="?", help="order id; omit to cancel all open orders")
    sp.set_defaults(func=cmd_cancel)

    for cname, fn in (("buy", cmd_buy), ("sell", cmd_sell)):
        sp = sub.add_parser(cname, help=f"{cname} ETF shares (subject to the risk guard)")
        sp.add_argument("code")
        g = sp.add_mutually_exclusive_group(required=True)
        g.add_argument("--qty", type=int, help="shares (rounded down to 100-lot)")
        g.add_argument("--amount", type=float, help="notional in CNY")
        sp.add_argument("--price", type=float, default=0.0, help="limit price; default = bid1/ask1")
        sp.add_argument("--reason", default="", help="free-text reason recorded in the audit log")
        sp.add_argument("--force", action="store_true", help="bypass the trading-hours check")
        sp.add_argument("--dry-run", action="store_true", help="run the guard, place nothing")
        sp.set_defaults(func=fn)

    for cname, fn, helptext in (
        ("plan", cmd_plan, "dry-run a strategy tick (never orders)"),
        ("run", cmd_run, "one strategy tick, submitting orders"),
        ("watch", cmd_watch, "loop a strategy tick"),
    ):
        sp = sub.add_parser(cname, help=helptext)
        sp.add_argument("strategy", choices=["grid", "t"])
        sp.add_argument("--code", required=True)
        sp.add_argument("--base", type=float, default=1.092, help="grid: anchor price")
        sp.add_argument("--spacing", type=float, default=3.0, help="grid: %% between levels")
        sp.add_argument("--levels-up", type=int, default=4, dest="levels_up")
        sp.add_argument("--levels-down", type=int, default=4, dest="levels_down")
        sp.add_argument("--qty", type=int, default=2800, help="shares per grid level / T leg")
        sp.add_argument("--lower", type=float, default=0.0, help="grid: hard lower bound")
        sp.add_argument("--upper", type=float, default=0.0, help="grid: hard upper bound")
        sp.add_argument("--trigger", type=float, default=1.5, help="t: %% move from reference")
        sp.add_argument("--take-profit", type=float, default=1.5, dest="take_profit", help="t: %% to close a leg")
        sp.add_argument("--price", type=float, default=0.0,
                        help="override the current price. Needed when the broker has no quote "
                             "feed (easytrader) and the code is not held.")
        sp.add_argument("--t0", action=argparse.BooleanOptionalAction, default=True,
                        help="t: fund is T+0 (allows 正T); use --no-t0 for T+1 funds")
        sp.add_argument("--force", action="store_true")
        sp.add_argument("--dry-run", action="store_true")
        if cname == "watch":
            sp.add_argument("--interval", type=float, default=30.0, help="seconds between ticks")
            sp.add_argument("--iterations", type=int, default=10, help="how many ticks (bounded)")
        sp.set_defaults(func=fn)

    sp = sub.add_parser("grid", help="grid helpers")
    gsub = sp.add_subparsers(dest="grid_command", required=True)
    gl = gsub.add_parser("ladder", help="print the price ladder for a grid")
    gl.add_argument("--code", default="513050")
    gl.add_argument("--base", type=float, default=1.092)
    gl.add_argument("--spacing", type=float, default=3.0)
    gl.add_argument("--levels-up", type=int, default=4, dest="levels_up")
    gl.add_argument("--levels-down", type=int, default=4, dest="levels_down")
    gl.add_argument("--qty", type=int, default=2800)
    gl.set_defaults(func=cmd_grid_ladder)

    sp = sub.add_parser("audit", help="recent audit events")
    sp.add_argument("--tail", type=int, default=20)
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("simulate", help="run a strategy over a synthetic price path (offline, no broker)")
    sp.add_argument("--strategy", choices=["grid", "t"], default="grid",
                    help="which shipped strategy to simulate")
    sp.add_argument("--code", default="513050")
    sp.add_argument("--base", type=float, default=1.092, help="anchor/start price")
    sp.add_argument("--spacing", type=float, default=3.0, help="grid: %% between levels")
    sp.add_argument("--trigger", type=float, default=1.5, help="t: %% move from reference to open a leg")
    sp.add_argument("--take-profit", type=float, default=1.5, dest="take_profit",
                    help="t: %% move to close a leg")
    sp.add_argument("--t0", action=argparse.BooleanOptionalAction, default=True,
                    help="t: fund is T+0 (allows 正T); use --no-t0 for T+1")
    sp.add_argument("--qty", type=int, default=2800, help="grid: shares per level; t: shares per leg")
    sp.add_argument("--levels-up", type=int, default=4, dest="levels_up")
    sp.add_argument("--levels-down", type=int, default=4, dest="levels_down")
    sp.add_argument("--cash", type=float, default=50000.0, help="starting cash")
    sp.add_argument("--position", type=int, default=0, help="starting shares (default: 40%% of cash)")
    sp.add_argument("--ticks", type=int, default=480, help="number of price ticks")
    sp.add_argument("--scenario", choices=["range", "up", "down", "all"], default="all",
                    help="price regime; 'all' runs all three and compares")
    sp.add_argument("--commission-rate", type=float, default=0.00015, dest="commission_rate",
                    help="per-side commission rate (0.00015 = 万1.5)")
    sp.add_argument("--min-commission", type=float, default=5.0, dest="min_commission",
                    help="per-order minimum commission (¥5 typical)")
    sp.add_argument("--no-min-commission", action="store_true", dest="no_min_commission",
                    help="model the case where the broker waives the ¥5 minimum ('免5')")
    sp.add_argument("--compare-fees", action="store_true", dest="compare_fees",
                    help="grid only: run with and without the minimum fee, side by side")
    sp.add_argument("--seed", type=int, default=7)
    sp.set_defaults(func=cmd_simulate)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    # pywinauto emits a UserWarning on import ("Revert to STA COM threading
    # mode") that would otherwise clutter stderr on every live command. The
    # behaviour is correct for us, so silence just that message and keep stdout
    # as the single JSON channel.
    import warnings

    warnings.filterwarnings("ignore", message=".*STA COM threading mode.*")
    warnings.filterwarnings("ignore", category=UserWarning, module="pywinauto")

    parser = build_parser()
    args = parser.parse_args(argv)
    app = App(args)
    try:
        args.func(app, args)
    except EtfaError as exc:
        fail(exc.code, str(exc))
    except KeyboardInterrupt:
        fail("interrupted", "interrupted by user", exit_code=130)
    except Exception as exc:  # noqa: BLE001 - last-resort JSON error
        fail("unexpected", f"{type(exc).__name__}: {exc}", exit_code=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
