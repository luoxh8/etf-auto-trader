"""Live adapter: drive the already-logged-in 同花顺-kernel desktop client.

Why this design at all
----------------------
国元证券 ships two branded desktop clients, and 国元远航 is the 同花顺-kernel
build (its original name was literally "国元证券同花顺"). easytrader supports
this family through two clienttypes:

* ``ths``               — the broker-branded 同花顺 client (xiadan.exe). Requires
                          several client-specific UI-automation classes
                          (e.g. ``ht_client`` for 华泰, ``gj_client`` for 国金).
* ``universal_client``  — the **generic** 同花顺 client; drives login itself.

国元 has no bespoke easytrader class, so ``ths`` will only work if 国元远航's
control tree happens to match a supported layout; otherwise the generic path
is the fallback. Both require the user to have **logged in by hand once** —
that is the whole point of this design: no password is ever stored here.

Because the two clienttypes have different call surfaces, this adapter probes
for the methods it needs at runtime instead of assuming one. When a required
capability is missing it raises ``UnsupportedFeature`` with a message telling
the operator exactly which method was absent, rather than reporting a
mysterious failure mid-order.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ..config import Config, broker_password
from ..errors import BrokerError, NotConnected, UnsupportedFeature
from ..models import (
    Balance,
    Order,
    Position,
    Quote,
    SIDE_BUY,
    SIDE_SELL,
    STATUS_NEW,
    now_iso,
)
from .base import BrokerAdapter


def _pick(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present, non-None value among ``keys`` (case-tolerant)."""
    lowered = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
        lk = k.lower()
        if lk in lowered and lowered[lk] is not None:
            return lowered[lk]
    return default


def _to_float(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("元", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _to_int(v: Any) -> int:
    return int(_to_float(v))


# Logical fields and the Chinese/English column names that may carry them.
# A field counts as "found" when ANY of its aliases is present, so a client that
# writes 可用金额 where another writes 可用余额 is not reported as a mismatch.
# `probe` uses these to diagnose a live client.
BALANCE_FIELDS = (
    ("cash", ("资金余额", "可用金额", "资金可用", "enable_balance")),
    ("available", ("可用金额", "可用余额", "可用", "enable_balance")),
    ("market_value", ("最新市值", "总市值", "股票市值", "market_value")),
    ("total", ("总资产", "资产总值", "asset_balance")),
    ("frozen", ("冻结金额", "冻结资金", "frozen_balance")),
)
POSITION_FIELDS = (
    ("code", ("证券代码", "股票代码", "code")),
    ("name", ("证券名称", "股票名称", "name")),
    ("qty", ("股票余额", "证券数量", "当前持仓", "stock_balance")),
    ("available", ("可用余额", "可用数量", "可卖数量", "enable_balance")),
    ("cost", ("参考成本价", "成本价", "cost_price")),
    ("last", ("市价", "最新价", "参考市价", "last")),
)
ENTRUST_FIELDS = (
    ("id", ("委托编号", "entrust_no")),
    ("code", ("证券代码", "股票代码", "code")),
    ("side", ("操作", "买卖标志")),
    ("price", ("委托价格", "委托均价", "entrust_price")),
    ("qty", ("委托数量", "entrust_bs_count")),
    ("filled", ("成交数量", "business_count")),
    ("status", ("委托状态", "备注", "status")),
)


def _match_fields(columns: list, fields) -> Dict[str, Any]:
    """For each logical field, report whether any alias matched and which one."""
    lowered = {c.lower(): c for c in columns}
    found, missing = {}, []
    for field, aliases in fields:
        hit = next((lowered[a.lower()] for a in aliases if a.lower() in lowered), None)
        if hit:
            found[field] = hit
        else:
            missing.append(field)
    return {"found": found, "missing": missing, "aliases": {f: list(a) for f, a in fields}}


def detect_client_window_title(exe_path: str, _enumerator=None) -> Optional[str]:
    """Read the main window title of the running client at ``exe_path``.

    Why this exists: easytrader's ``connect()`` calls ``_close_prompt_windows()``,
    which **closes every visible window of class ``#32770`` whose title differs
    from its configured ``TITLE``** (``'网上股票交易系统5.0'`` for generic
    ``ths``). The 同花顺-family main window IS class ``#32770``, so a branded
    client with a different title would have its main window closed at connect
    time. Detecting the real title first and pinning it prevents that.

    Read-only: enumerates windows, never clicks or types. ``_enumerator`` is a
    test seam.
    """
    if _enumerator is not None:
        return _enumerator(exe_path)
    try:
        import win32gui
        import win32process
        import psutil
    except Exception:  # noqa: BLE001 - missing pieces = cannot detect, not fatal
        return None

    base = os.path.basename(exe_path).lower()
    pids = set()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info.get("name") or "").lower() == base:
                pids.add(proc.info["pid"])
        except Exception:  # noqa: BLE001
            continue
    if not pids:
        return None

    found: List[tuple] = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid not in pids:
                return
            title = win32gui.GetWindowText(hwnd) or ""
            if title:
                found.append((win32gui.GetClassName(hwnd), title))
        except Exception:  # noqa: BLE001
            return

    win32gui.EnumWindows(_cb, None)
    if not found:
        return None
    # the classic dialog class is what easytrader's control ids live under
    for cls, title in found:
        if cls == "#32770":
            return title
    return found[0][1]


class EasytraderThsBroker(BrokerAdapter):
    name = "easytrader_ths"

    def __init__(self, cfg: Config, trader: Any = None):
        self.cfg = cfg
        # `trader` lets tests inject a stand-in for easytrader's ClientTrader;
        # in production it is built lazily in connect().
        self._user: Any = trader
        self._connected = bool(trader)
        self._title_source = "not connected"

    def _pin_window_title(self, exe: str) -> None:
        """Set the expected window title on the easytrader config, if we can.

        Precedence: an explicit ``broker.expected_title`` wins; otherwise we
        auto-detect from the running client. If both fail we leave easytrader's
        default alone but record it, so ``probe`` can explain what happened.
        """
        self._title_source = "easytrader default"
        title = self.cfg.broker.expected_title or detect_client_window_title(exe)
        if not title:
            return
        self._title_source = (
            "config" if self.cfg.broker.expected_title else "auto-detected"
        )
        try:
            self._user.config.TITLE = title
        except Exception:  # noqa: BLE001 - never block connecting over this
            self._title_source = "failed to apply"

    # ---- connection -------------------------------------------------------

    def connect(self) -> None:
        clienttype = self.cfg.broker.clienttype

        # Build the easytrader object only if one was not injected.
        if self._user is None:
            try:
                import easytrader  # type: ignore
            except ImportError as exc:  # pragma: no cover - exercised only when missing
                raise BrokerError(
                    "easytrader is not installed. Install the live extra:\n"
                    "  pip install -e \".[live]\"  (from the repo, using its venv)\n"
                    "Note: if pip is pointed at a mirror that lacks easytrader, pass "
                    "-i https://pypi.org/simple"
                ) from exc
            try:
                self._user = easytrader.use(clienttype)
            except Exception as exc:  # noqa: BLE001 - easytrader raises bare exceptions
                raise BrokerError(f"easytrader.use({clienttype!r}) failed: {exc}") from exc

        if clienttype == "ths":
            exe = self.cfg.broker.exe_path
            if not exe:
                raise BrokerError(
                    "broker.exe_path must point at the 同花顺/国元远航 client executable "
                    "(xiadan.exe / hexin.exe) when clienttype='ths'"
                )
            # Pin the expected window title BEFORE attaching. easytrader's
            # connect() closes every #32770 window whose title differs from this
            # value, and the main trading window is #32770 — so a branded client
            # (国元远航) with a different title would have its main window closed.
            self._pin_window_title(exe)
            if not self._connected:
                try:
                    self._user.connect(exe)
                except Exception as exc:  # noqa: BLE001
                    raise NotConnected(
                        f"could not attach to the client at {exe}: {exc}. "
                        "Make sure the client is OPEN and LOGGED IN, then retry."
                    ) from exc
        else:  # universal_client drives login from the credentials
            if not self._connected:
                user = self.cfg.broker.user
                password = broker_password(self.cfg)
                if not user or not password:
                    raise BrokerError(
                        "clienttype='universal_client' needs broker.user and the password in "
                        f"the ${self.cfg.broker.password_env} environment variable"
                    )
                try:
                    self._user.prepare(user=user, password=password, exe_path=self.cfg.broker.exe_path)
                except Exception as exc:  # noqa: BLE001
                    raise NotConnected(f"universal_client login failed: {exc}") from exc

        self._require_capability("balance")
        self._connected = True

    def is_connected(self) -> bool:
        return self._connected

    def close(self) -> None:
        self._connected = False

    def _require_capability(self, *names: str) -> None:
        if self._user is None:
            raise NotConnected("not connected")
        missing = [n for n in names if not hasattr(self._user, n)]
        if missing:
            raise UnsupportedFeature(
                f"easytrader clienttype '{self.cfg.broker.clienttype}' on this client lacks "
                f"{missing}. The branded 国元 client is not fully supported by easytrader yet; "
                f"see docs/COMPATIBILITY.md for the generic-client fallback."
            )

    def _call(self, method: str, *args, **kwargs) -> Any:
        """Invoke a *method* capability (buy/sell/cancel/connect)."""
        self._require_capability(method)
        fn = getattr(self._user, method)
        if not callable(fn):
            raise UnsupportedFeature(
                f"easytrader attribute '{method}' is not callable on this client"
            )
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"easytrader {method} failed: {exc}") from exc

    def _attr(self, name: str) -> Any:
        """Read a capability that easytrader exposes EITHER as a property or a method.

        This matters: in easytrader 0.23.7 `balance`, `position`, `today_entrusts`
        and `cancel_entrusts` are *properties* (verified by introspection), so
        `getattr` already returns the value. Some subclasses wrap them in
        methods instead. Handle both so a client that differs does not crash us
        with "'dict' object is not callable".
        """
        self._require_capability(name)
        value = getattr(self._user, name)
        if callable(value):
            try:
                return value()
            except Exception as exc:  # noqa: BLE001
                raise BrokerError(f"easytrader {name}() failed: {exc}") from exc
        return value

    # ---- reads ------------------------------------------------------------

    def balance(self) -> Balance:
        raw = self._attr("balance")
        if isinstance(raw, list):
            raw = raw[0] if raw else {}
        raw = raw or {}
        cash = _to_float(_pick(raw, "资金余额", "可用金额", "可用余额", "资金可用", "enable_balance"))
        available = _to_float(_pick(raw, "可用金额", "可用余额", "可用", "enable_balance", default=cash))
        mv = _to_float(_pick(raw, "最新市值", "总市值", "股票市值", "market_value"))
        total = _to_float(_pick(raw, "总资产", "资产总值", "asset_balance", default=0.0)) or (cash + mv)
        frozen = _to_float(_pick(raw, "冻结金额", "冻结资金", "frozen_balance"))
        return Balance(cash=cash, available=available, market_value=mv, total=total, frozen=frozen)

    def positions(self) -> List[Position]:
        raw = self._attr("position")
        out: List[Position] = []
        for row in raw or []:
            if not isinstance(row, dict):
                continue
            code = str(_pick(row, "证券代码", "股票代码", "code", default="")).strip()
            if not code:
                continue
            out.append(
                Position(
                    code=code,
                    name=str(_pick(row, "证券名称", "股票名称", "name", default="")),
                    qty=_to_int(_pick(row, "股票余额", "证券数量", "当前持仓", "stock_balance")),
                    available=_to_int(_pick(row, "可用余额", "可用数量", "可卖数量", "enable_balance")),
                    cost=_to_float(_pick(row, "参考成本价", "成本价", "参考市价", "cost_price")),
                    last=_to_float(_pick(row, "市价", "最新价", "参考市价", "last")),
                )
            )
        return out

    def quote(self, codes: List[str]) -> Dict[str, Quote]:
        """Best-effort, and honest about its limits.

        easytrader exposes **no quote feed** (verified: ClientTrader has no
        price/quote method; only market_buy/market_sell). The only price the
        client already knows is the market-price column of the position grid,
        so we read that. A code that is not held therefore has NO quote here —
        callers must supply an explicit price (CLI: --price).
        """
        by_code = {p.code: p for p in self.positions()}
        out: Dict[str, Quote] = {}
        for c in codes:
            p = by_code.get(c)
            if p and p.last:
                out[c] = Quote(code=c, name=p.name, last=p.last, prev_close=p.last, ts=now_iso())
        return out

    def orders(self, open_only: bool = False) -> List[Order]:
        raw = self._attr("today_entrusts")
        out: List[Order] = []
        for row in raw or []:
            if not isinstance(row, dict):
                continue
            qty = _to_int(_pick(row, "委托数量", "entrust_bs_count"))
            filled = _to_int(_pick(row, "成交数量", "business_count"))
            status_raw = str(_pick(row, "委托状态", "备注", "status", default=""))
            o = Order(
                id=str(_pick(row, "委托编号", "entrust_no", default="")),
                code=str(_pick(row, "证券代码", "股票代码", "code", default="")),
                side=SIDE_BUY if str(_pick(row, "操作", "买卖标志", default="")).startswith("买") else SIDE_SELL,
                price=_to_float(_pick(row, "委托价格", "委托均价", "entrust_price")),
                qty=qty,
                filled=filled,
                status=_map_status(status_raw),
                ts=now_iso(),
            )
            if open_only and not o.is_open:
                continue
            out.append(o)
        return out

    # ---- diagnostics ------------------------------------------------------

    def probe(self) -> Dict[str, Any]:
        """Report the RAW client payloads and whether our column mapping matches.

        This is the tool for the one thing that cannot be tested offline: whether
        the 国元 client's control tree and column names are what easytrader (and
        therefore this adapter) expects. It reads, it never writes, and it shows
        exactly which expected Chinese columns came back unmatched.

        It also reports the *window identity* check, because easytrader's generic
        `ths` config is pinned to a specific window title and control-id set:
        if the branded client's title differs, that is the first thing to look at.
        """
        out: Dict[str, Any] = {"clienttype": self.cfg.broker.clienttype}

        # --- window identity: the hard-coded expectations in easytrader's ths config
        expected_title = None
        balance_ids = None
        try:
            cfg_obj = self._user.config
            expected_title = getattr(cfg_obj, "TITLE", None)
            balance_ids = getattr(cfg_obj, "BALANCE_CONTROL_ID_GROUP", None)
        except Exception:  # noqa: BLE001 - a missing config must not break the probe
            pass

        actual_title = None
        try:
            main = self._user.main
            if main is not None:
                actual_title = main.window_text()
        except Exception:  # noqa: BLE001
            pass

        out["window"] = {
            "expected_title": expected_title,
            "actual_title": actual_title,
            "title_match": (
                (actual_title == expected_title)
                if (expected_title is not None and actual_title is not None)
                else None
            ),
            "title_source": self._title_source,
            "expected_balance_control_ids": balance_ids,
            "note": (
                "easytrader's 'ths' config is pinned to this window title and control-id set. "
                "This adapter pins the title BEFORE connecting, because easytrader's connect() "
                "closes every #32770 window whose title differs — and the main trading window "
                "is #32770. title_source says where the pinned value came from."
            ),
        }
        # menu paths this adapter navigates, so a mismatch is easy to eyeball
        out["menu_paths_used"] = [
            ["查询[F4]", "资金股票"],
            ["查询[F4]", "当日委托"],
            ["买入[F1]"],
            ["卖出[F2]"],
            ["撤单[F3]"],
        ]

        for name, fields in (
            ("balance", BALANCE_FIELDS),
            ("position", POSITION_FIELDS),
            ("today_entrusts", ENTRUST_FIELDS),
        ):
            try:
                raw = self._attr(name)
            except EtfaError as exc:
                out[name] = {"error": exc.code, "message": str(exc)}
                continue
            if isinstance(raw, list):
                row = raw[0] if raw else {}
                count = len(raw)
            elif isinstance(raw, dict):
                row, count = raw, 1
            else:
                row, count = {}, 0
            cols = sorted(str(k) for k in row.keys()) if isinstance(row, dict) else []
            out[name] = {
                "type": type(raw).__name__,
                "row_count": count,
                "columns_returned": cols,
                "mapping": _match_fields(cols, fields),
                "sample": row,
            }
        return out

    # ---- writes -----------------------------------------------------------

    def buy(self, code: str, price: float, qty: int) -> Order:
        # signature is buy(self, security, price, amount) -> pass positionally
        result = self._call("buy", code, price, qty)
        return _order_from_result(result, code, SIDE_BUY, price, qty)

    def sell(self, code: str, price: float, qty: int) -> Order:
        result = self._call("sell", code, price, qty)
        return _order_from_result(result, code, SIDE_SELL, price, qty)

    def cancel(self, order_id: Optional[str] = None) -> List[str]:
        if order_id:
            self._call("cancel_entrust", order_id)
            return [order_id]
        cancelled = []
        for o in self.orders(open_only=True):
            try:
                self._call("cancel_entrust", o.id)
                cancelled.append(o.id)
            except BrokerError:
                continue
        return cancelled


def _map_status(raw: str) -> str:
    raw = raw or ""
    if "已成" in raw or raw in ("2", "已成"):
        return "filled"
    if "部成" in raw:
        return "partial"
    if "已撤" in raw or "撤单" in raw:
        return "cancelled"
    if "废" in raw or "拒" in raw:
        return "rejected"
    if "已报" in raw or "待" in raw:
        return "reported"
    return STATUS_NEW


def _order_from_result(result: Any, code: str, side: str, price: float, qty: int) -> Order:
    """Normalise easytrader's reply. It is usually a dict like
    {'entrust_no': '12345'} or {'message': '...'} on failure.

    Fail closed: an unrecognised payload means we do NOT know whether the order
    was accepted, so the order is marked rejected and the caller sees it rather
    than assuming success.
    """
    oid = ""
    msg = ""
    ok = False
    if isinstance(result, dict):
        oid = str(_pick(result, "entrust_no", "委托编号", "order_id", default="") or "")
        msg = str(_pick(result, "message", "备注", default="") or "")
        ok = bool(oid)
    return Order(
        id=oid,
        code=code,
        side=side,
        price=price,
        qty=qty,
        status=STATUS_NEW if ok else "rejected",
        ts=now_iso(),
        message=msg or ("" if ok else f"unexpected reply: {result!r}"),
    )
