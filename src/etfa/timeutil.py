"""Trading-session helpers.

A-share ETF continuous auction is 09:30-11:30 and 13:00-15:00 on weekdays.
This module intentionally ships **no holiday calendar** (that would mean a
data dependency); instead ``is_trading_time`` is a *weekday + clock* check
that is deliberately conservative on the safe side — it can only say
"probably open". The real gate against trading on a holiday is the broker
itself (an order on a closed day is simply rejected), so we do not pretend
to know the exchange calendar. If you want exact sessions, point
``config.trading.holidays_file`` at a list of YYYY-MM-DD closure dates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, List, Optional, Set

MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)

# Auction windows worth knowing about (order entry is allowed, matching may not happen).
OPENING_AUCTION = (time(9, 15), time(9, 25))
CLOSING_AUCTION = (time(14, 57), time(15, 0))


def _load_holidays(path: Optional[str]) -> Set[str]:
    if not path:
        return set()
    p = Path(path)
    if not p.exists():
        return set()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if isinstance(raw, dict) and "holidays" in raw:
        raw = raw["holidays"]
    return {str(x) for x in raw} if isinstance(raw, Iterable) else set()


@dataclass
class Session:
    is_weekday: bool
    is_holiday: bool
    in_continuous: bool
    in_auction: bool
    label: str

    @property
    def tradable(self) -> bool:
        return self.is_weekday and not self.is_holiday and (self.in_continuous or self.in_auction)

    def to_dict(self) -> dict:
        return {
            "is_weekday": self.is_weekday,
            "is_holiday": self.is_holiday,
            "in_continuous": self.in_continuous,
            "in_auction": self.in_auction,
            "tradable": self.tradable,
            "label": self.label,
        }


def classify(dt: Optional[datetime] = None, holidays_file: Optional[str] = None) -> Session:
    dt = dt or datetime.now()
    is_weekday = dt.weekday() < 5
    is_holiday = dt.strftime("%Y-%m-%d") in _load_holidays(holidays_file)
    t = dt.time()

    in_continuous = (MORNING_OPEN <= t < MORNING_CLOSE) or (AFTERNOON_OPEN <= t < AFTERNOON_CLOSE)
    in_auction = (OPENING_AUCTION[0] <= t < OPENING_AUCTION[1]) or (CLOSING_AUCTION[0] <= t < CLOSING_AUCTION[1])

    if is_holiday:
        label = "holiday"
    elif not is_weekday:
        label = "weekend"
    elif in_continuous:
        label = "continuous"
    elif in_auction:
        label = "auction"
    elif t < MORNING_OPEN:
        label = "pre_open"
    elif MORNING_CLOSE <= t < AFTERNOON_OPEN:
        label = "lunch_break"
    else:
        label = "closed"

    return Session(is_weekday, is_holiday, in_continuous, in_auction, label)


def is_trading_time(dt: Optional[datetime] = None, holidays_file: Optional[str] = None) -> bool:
    return classify(dt, holidays_file).tradable


def next_open_after(dt: Optional[datetime] = None) -> datetime:
    """First continuous-auction instant strictly after ``dt`` (weekday-only)."""
    dt = dt or datetime.now()
    candidate = dt
    for _ in range(10):
        if candidate.weekday() >= 5:
            candidate = datetime.combine(candidate.date() + timedelta(days=1), MORNING_OPEN)
            continue
        t = candidate.time()
        if t < MORNING_OPEN:
            return datetime.combine(candidate.date(), MORNING_OPEN)
        if t < AFTERNOON_OPEN:
            return datetime.combine(candidate.date(), AFTERNOON_OPEN) if t >= MORNING_CLOSE else candidate
        candidate = datetime.combine(candidate.date() + timedelta(days=1), MORNING_OPEN)
    return candidate


def session_note(holidays_file: Optional[str] = None) -> str:
    s = classify(holidays_file=holidays_file)
    mapping = {
        "continuous": "盘中（连续竞价）",
        "auction": "集合竞价",
        "pre_open": "开盘前",
        "lunch_break": "午间休市",
        "closed": "已收盘",
        "weekend": "周末休市",
        "holiday": "节假日休市",
    }
    return mapping.get(s.label, s.label)
