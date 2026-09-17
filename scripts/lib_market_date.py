#!/usr/bin/env python3
"""
lib_market_date.py — THE shared US-market-date helper.

Every job, script, and row stamp uses this instead of date.today() or NOW().
Rationale (learned on the Integrity Compounders migration): GitHub Actions and
Postgres run in UTC. A job fired after 8pm Eastern stamps *tomorrow's* date if
it uses the runner clock, and any "latest snapshot" query then returns an empty
or stale set. One helper, imported everywhere, ends that class of bug.

Usage:
    from lib_market_date import us_market_date, is_market_hours
    stamp = us_market_date()          # date of the current/most recent US session
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover — py<3.9 fallback, fixed offset is wrong half the year
    _ET = timezone(timedelta(hours=-5))

# NYSE full-day holidays. Extend each December for the following year.
_HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def _is_trading_day(d) -> bool:
    return d.weekday() < 5 and d.isoformat() not in _HOLIDAYS


def us_market_date(now: Optional[datetime] = None):
    """Date of the current US session if markets have opened today (ET),
    else the most recent completed session. This is the ONLY date that may be
    written to snapshot_date / stack_date / price date columns."""
    now_et = (now or datetime.now(timezone.utc)).astimezone(_ET)
    d = now_et.date()
    # Before 9:30 ET, "today's session" hasn't happened — use the prior session.
    if now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 30):
        d = d - timedelta(days=1)
    while not _is_trading_day(d):
        d = d - timedelta(days=1)
    return d


def is_market_hours(now: Optional[datetime] = None) -> bool:
    """True during the regular US session (9:30–16:00 ET, trading days)."""
    now_et = (now or datetime.now(timezone.utc)).astimezone(_ET)
    if not _is_trading_day(now_et.date()):
        return False
    mins = now_et.hour * 60 + now_et.minute
    return 9 * 60 + 30 <= mins <= 16 * 60


if __name__ == "__main__":
    print("us_market_date:", us_market_date())
    print("is_market_hours:", is_market_hours())
