from datetime import datetime
from zoneinfo import ZoneInfo

from simplebot import sessions


def test_market_for_and_currency():
    assert sessions.market_for("QQQ") is sessions.US
    assert sessions.market_for("vusa.l") is sessions.UK
    assert sessions.currency_for("VUSA.L") == "GBP" and sessions.currency_for("SPY") == "USD"


def test_minutes_to_close_and_session_checks():
    ny = ZoneInfo("America/New_York")
    ts = datetime(2026, 10, 5, 15, 50, tzinfo=ny)  # Monday
    assert sessions.minutes_to_close(sessions.US, ts) == 10
    assert sessions.is_in_session(sessions.US, ts)
    assert not sessions.is_in_session(sessions.US, datetime(2026, 10, 4, 12, 0, tzinfo=ny))
    assert not sessions.is_in_session(sessions.US, datetime(2026, 10, 5, 16, 0, tzinfo=ny))
    assert sessions.minutes_since_open(sessions.US, ts) == 380
    ldn = ZoneInfo("Europe/London")
    assert sessions.minutes_to_close(sessions.UK, datetime(2026, 10, 5, 16, 0, tzinfo=ldn)) == 30
    nxt = sessions.next_session_open(sessions.US, datetime(2026, 10, 3, 12, 0, tzinfo=ny))
    assert nxt.weekday() == 0 and nxt.hour == 9 and nxt.minute == 30
