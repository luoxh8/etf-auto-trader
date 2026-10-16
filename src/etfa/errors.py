"""Exception hierarchy.

Every failure the CLI can surface is one of these, so callers (and an AI
driving the CLI) can branch on the exception class rather than parsing text.
The CLI maps each to a stable ``error`` string in its JSON output.
"""

from __future__ import annotations


class EtfaError(Exception):
    """Base class. ``code`` is a stable, machine-readable identifier."""

    code = "etfa_error"


class ConfigError(EtfaError):
    """Configuration is missing, malformed, or internally inconsistent."""

    code = "config_error"


class BrokerError(EtfaError):
    """The broker layer failed (client missing, not logged in, bad reply)."""

    code = "broker_error"


class NotConnected(BrokerError):
    """No usable link to the trading client."""

    code = "not_connected"


class RiskRejected(EtfaError):
    """A pre-trade guardrail refused the order. The message says which one."""

    code = "risk_rejected"


class TradingHoursError(EtfaError):
    """Order attempted outside a tradable session and ``--force`` was not set."""

    code = "outside_trading_hours"


class UnsupportedFeature(EtfaError):
    """The selected adapter cannot do what was asked (e.g. QMT stub)."""

    code = "unsupported_feature"
