"""保留时长解析与到期计算测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.retention import add_period, parse_period


@pytest.mark.parametrize(
    ("text", "expected_canonical"),
    [
        ("90", "P90D"),
        ("90d", "P90D"),
        ("6mo", "P6M"),
        ("1y", "P1Y"),
        ("P1Y2M30D", "P1Y2M30D"),
        ("p90d", "P90D"),
        ("PT12H30M", "PT12H30M"),
        ("2w", "P2W"),
        ("indefinite", "indefinite"),
        ("INFINITE", "indefinite"),
    ],
)
def test_parse_valid_periods(text, expected_canonical):
    period, error = parse_period(text)
    assert error is None
    assert period is not None
    assert period.canonical() == expected_canonical


@pytest.mark.parametrize("text", ["", "  ", "abc", "P", "PT", "1.5d", "90x", "P90", "P-1D"])
def test_parse_invalid_periods(text):
    period, error = parse_period(text)
    assert period is None
    assert error


def test_month_calendar_arithmetic_end_of_month():
    base = datetime(2025, 1, 31, 10, 0, tzinfo=timezone.utc)
    period, _ = parse_period("1mo")
    result = add_period(base, period)
    # 1/31 加一个月落到 2/28（2025 非闰年）
    assert result == datetime(2025, 2, 28, 10, 0, tzinfo=timezone.utc)


def test_year_and_leap_day():
    base = datetime(2024, 2, 29, tzinfo=timezone.utc)
    period, _ = parse_period("1y")
    result = add_period(base, period)
    assert result == datetime(2025, 2, 28, tzinfo=timezone.utc)


def test_naive_datetime_treated_as_utc():
    base = datetime(2025, 1, 1, 0, 0)
    period, _ = parse_period("P1D")
    result = add_period(base, period)
    assert result.tzinfo is not None
    assert result == datetime(2025, 1, 2, tzinfo=timezone.utc)


def test_timezone_is_normalized_to_utc():
    base = datetime(2025, 1, 1, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    period, _ = parse_period("P1D")
    result = add_period(base, period)
    assert result == datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc)


def test_indefinite_has_no_expiry():
    period, _ = parse_period("indefinite")
    with pytest.raises(ValueError):
        add_period(datetime(2025, 1, 1, tzinfo=timezone.utc), period)
