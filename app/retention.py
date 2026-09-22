"""保留时长解析与到期时间计算。

支持的输入形式：
- "indefinite"（无限期，诉讼冻结等）
- ISO-8601 时长：PnYnMnWnDTnHnMnS，例如 P1Y2M30D、PT12H30M（不接受小数）
- 简写：数字+单位（y 年 / mo 月 / w 周 / d 天 / h 小时 / mi 分钟 / s 秒），
  单独一个纯数字按天处理，例如 90、"90d"、"6mo"
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

WILDCARD_PERIODS: set[str] = {"indefinite", "infinite", "forever", "permanent", "none", "-"}
_SHORTHAND_RE = re.compile(
    r"^(?P<value>\d+)\s*(?P<unit>mo|mi|y|m|w|d|h|s)?$",
    re.IGNORECASE,
)
# 用于在 ISO 与简写两种单位命名间消歧：M=月（日期部分），MI=分钟
_UNIT_ALIASES: dict[str, str] = {
    "y": "Y",
    "m": "M",   # 简写里单字母 m 一律按月处理；分钟请用 mi
    "mo": "M",
    "w": "W",
    "d": "D",
    "h": "H",
    "mi": "MIN",
    "s": "S",
}
_UNIT_LABELS_ZH: dict[str, str] = {
    "Y": "年",
    "M": "个月",
    "W": "周",
    "D": "天",
    "H": "小时",
    "MIN": "分钟",
    "S": "秒",
}
_UNIT_ORDER = ["Y", "M", "W", "D", "H", "MIN", "S"]


@dataclass(frozen=True)
class Period:
    years: int = 0
    months: int = 0
    weeks: int = 0
    days: int = 0
    hours: int = 0
    minutes: int = 0
    seconds: int = 0
    indefinite: bool = False

    def is_zero(self) -> bool:
        return not self.indefinite and not any(
            (self.years, self.months, self.weeks, self.days, self.hours, self.minutes, self.seconds)
        )

    def canonical(self) -> str:
        if self.indefinite:
            return "indefinite"
        date_bits = (
            f"{self.years}Y" if self.years else "",
            f"{self.months}M" if self.months else "",
            f"{self.weeks}W" if self.weeks else "",
            f"{self.days}D" if self.days else "",
        )
        time_bits = (
            f"{self.hours}H" if self.hours else "",
            f"{self.minutes}M" if self.minutes else "",
            f"{self.seconds}S" if self.seconds else "",
        )
        date_part = "".join(date_bits)
        time_part = "".join(time_bits)
        body = "P"
        if date_part:
            body += date_part
        if time_part:
            body += f"T{time_part}"
        return body if body != "P" else "P0D"

    def describe(self) -> str:
        if self.indefinite:
            return "无限期"
        if self.is_zero():
            return "0 天（立即到期）"
        parts = [
            f"{value}{_UNIT_LABELS_ZH[name]}"
            for name in _UNIT_ORDER
            if (value := getattr(self, _FIELD_BY_UNIT[name]))
        ]
        return "、".join(parts)


_FIELD_BY_UNIT = {
    "Y": "years",
    "M": "months",
    "W": "weeks",
    "D": "days",
    "H": "hours",
    "MIN": "minutes",
    "S": "seconds",
}


def parse_period(raw: str) -> tuple[Period | None, str | None]:
    """解析保留时长文本。

    成功返回 (Period, None)；失败返回 (None, 错误说明)。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, "保留时长不能为空"
    text = raw.strip()
    if text.lower() in WILDCARD_PERIODS:
        return Period(indefinite=True), None

    upper = text.upper()
    if upper.startswith("P"):
        return _parse_iso8601(upper)
    return _parse_shorthand(text)


def _parse_iso8601(text: str) -> tuple[Period | None, str | None]:
    match = re.fullmatch(
        r"P"
        r"(?:(?P<years>\d+)Y)?"
        r"(?:(?P<months>\d+)M)?"
        r"(?:(?P<weeks>\d+)W)?"
        r"(?:(?P<days>\d+)D)?"
        r"(?:T"
        r"(?:(?P<hours>\d+)H)?"
        r"(?:(?P<minutes>\d+)M)?"
        r"(?:(?P<seconds>\d+)S)?"
        r")?",
        text,
    )
    if not match:
        return None, f"不符合 ISO-8601 时长格式：{text}（示例 P1Y2M30D、PT12H）"
    values = {key: int(value) for key, value in match.groupdict(default="0").items()}
    if not any(values.values()):
        return None, f"ISO-8601 时长至少要包含一个单位：{text}"
    return Period(**values), None


def _parse_shorthand(text: str) -> tuple[Period | None, str | None]:
    match = _SHORTHAND_RE.fullmatch(text)
    if not match:
        return None, f"无法识别的保留时长：{text}（示例 90d、6mo、1y、12h）"
    value = int(match.group("value"))
    unit = (match.group("unit") or "d").lower()
    canonical_unit = _UNIT_ALIASES[unit]
    kwargs = {_FIELD_BY_UNIT[canonical_unit]: value}
    return Period(**kwargs), None


def _add_calendar_months(dt: datetime, months: int) -> datetime:
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    # 处理 1/31 + 1 月 → 2/28 这类月末溢出：落到目标月最后一天
    day = min(dt.day, _days_in_month(year, month))
    return dt.replace(year=year, month=month, day=day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return (next_month - timedelta(days=1)).day


def add_period(dt: datetime, period: Period) -> datetime:
    """在给定时间上加上保留时长。结果统一带 UTC 时区。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    if period.indefinite:
        raise ValueError("无限期保留没有到期时间")
    if period.years or period.months:
        dt = _add_calendar_months(dt, period.years * 12 + period.months)
    delta = timedelta(
        weeks=period.weeks,
        days=period.days,
        hours=period.hours,
        minutes=period.minutes,
        seconds=period.seconds,
    )
    return dt + delta


def period_seconds(period: Period) -> int | None:
    """估算保留秒数（月按 30 天、年按 365 天，仅用于元信息，不参与到期计算）。"""
    if period.indefinite:
        return None
    return (
        period.years * 365 * 86400
        + period.months * 30 * 86400
        + period.weeks * 7 * 86400
        + period.days * 86400
        + period.hours * 3600
        + period.minutes * 60
        + period.seconds
    )
