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


class EasytraderThsBroker(BrokerAdapter):
    name = "easytrader_ths"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._user: Any = None        # easytrader.use(...) result
        self._connected = False

    # ---- connection -------------------------------------------------------

    def connect(self) -> None:
        try:
            import easytrader  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised only when missing
            raise BrokerError(
                "easytrader is not installed. Install the live extra:\n"
                "  pip install -e \".[live]\"  (from the repo, using its venv)\n"
                "Note: if pip is pointed at a mirror that lacks easytrader, pass "
                "-i https://pypi.org/simple"
            ) from exc

        clienttype = self.cfg.broker.clienttype
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
            try:
                self._user.connect(exe)
            except Exception as exc:  # noqa: BLE001
                raise NotConnected(
                    f"could not attach to the client at {exe}: {exc}. "
                    "Make sure the client is OPEN and LOGGED IN, then retry."
                ) from exc
        else:  # universal_client drives login from the credentials
            user = self.cfg.broker.user
            password = broker_password(self.cfg)
            if not user or not password:
                raise BrokerError(
                    "clienttype='universal_client' needs broker.user and the password in "
                    f"the ${self.cfg.broker.password_env} environment variable"
                )
            self._user.prepare(user=user, password=password, exe_path=self.cfg.broker.exe_path)

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
        self._require_capability(method)
        fn = getattr(self._user, method)
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"easytrader {method} failed: {exc}") from exc

    # ---- reads ------------------------------------------------------------

    def balance(self) -> Balance:
        raw = self._call("balance")
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
        raw = self._call("position")
        out: List[Position] = []
        for row in raw or []:
            if not isinstance(row, dict):
                continue
            code = str(_pick(row, "证券代码", "股票代码", "code", "证券代码/名称", default="")).strip()
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
        """Best-effort: easytrader exposes no uniform quote feed.

        We read prices off the position/balance snapshot the client already
        has (market price column). If a code is not held, callers must pass an
        explicit ``--price``. This keeps the adapter honest: it never invents
        a price.
        """
        by_code = {p.code: p for p in self.positions()}
        out: Dict[str, Quote] = {}
        for c in codes:
            p = by_code.get(c)
            if p and p.last:
                out[c] = Quote(code=c, name=p.name, last=p.last, prev_close=p.last, ts=now_iso())
        return out

    def orders(self, open_only: bool = False) -> List[Order]:
        if not hasattr(self._user, "today_entrusts"):
            raise UnsupportedFeature("this easytrader clienttype has no today_entrusts()")
        raw = self._call("today_entrusts")
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

    # ---- writes -----------------------------------------------------------

    def buy(self, code: str, price: float, qty: int) -> Order:
        result: Any = None
        # easytrader ths: buy(security, price, amount)
        result = self._call("buy", code, price=price, amount=qty)
        return _order_from_result(result, code, SIDE_BUY, price, qty)

    def sell(self, code: str, price: float, qty: int) -> Order:
        result = self._call("sell", code, price=price, amount=qty)
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
