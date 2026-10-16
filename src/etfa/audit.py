"""Append-only JSONL journal.

Every decision the tool makes — each order intent, each guardrail rejection,
each broker reply — is written here as one JSON object per line, with a
monotonic sequence number. This is the audit trail that answers "why did the
bot buy at 10:07?" and it is the only state used for the daily-notional cap,
so it must never be rewritten in place.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class AuditEvent:
    ts: str
    kind: str            # intent | placed | rejected | cancel | note | error
    code: str = ""
    side: str = ""
    qty: int = 0
    price: float = 0.0
    amount: float = 0.0
    broker: str = ""
    order_id: str = ""
    ok: bool = True
    detail: str = ""
    extra: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "ts": self.ts,
            "kind": self.kind,
            "ok": self.ok,
        }
        for k in ("code", "side", "broker", "order_id", "detail"):
            v = getattr(self, k)
            if v:
                d[k] = v
        for k in ("qty", "price", "amount"):
            v = getattr(self, k)
            if v:
                d[k] = v
        if self.extra:
            d["extra"] = self.extra
        return d


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: AuditEvent) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        # single append write; O_APPEND keeps concurrent writers from interleaving
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out: List[Dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        if limit is not None:
            return out[-limit:]
        return out

    def tail(self, n: int = 20) -> List[Dict[str, Any]]:
        return self.read(limit=n)

    def placed_today(self, day: Optional[date] = None, broker: Optional[str] = None) -> List[Dict[str, Any]]:
        day = day or date.today()
        prefix = day.isoformat()
        out = []
        for e in self.read():
            if e.get("kind") != "placed" or not e.get("ok", True):
                continue
            if not str(e.get("ts", "")).startswith(prefix):
                continue
            if broker and e.get("broker") != broker:
                continue
            out.append(e)
        return out

    def notional_today(self, day: Optional[date] = None, broker: Optional[str] = None) -> float:
        return round(sum(float(e.get("amount") or 0.0) for e in self.placed_today(day, broker)), 2)
