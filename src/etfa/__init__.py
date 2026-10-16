"""etf-auto-trader: CLI-driven ETF auto-trading for a personal account.

The package is deliberately split so that everything except the final
"talk to the broker" step is pure standard-library logic and therefore
unit-testable without a brokerage client installed:

    models      plain dataclasses for quotes / orders / positions
    config      on-disk configuration (no credentials stored by default)
    risk        pre-trade guardrails (whitelist, size, hours, daily cap)
    audit       append-only JSONL journal of every intent and outcome
    broker/*    adapter layer: mock (offline) and easytrader_ths (live)
    strategies  grid and base-position T logic, pure functions of state
    cli         the `etfa` command surface designed for an AI to drive
"""

__version__ = "0.1.0"
