from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any

import yaml


_HHMM = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"calendar date must be YYYY-MM-DD, got {value!r}") from exc


def _parse_hhmm(value: Any, *, allow_2400: bool = False) -> int:
    text = str(value).strip()
    if allow_2400 and text == "24:00":
        return 24 * 60
    if not _HHMM.fullmatch(text):
        suffix = " or 24:00" if allow_2400 else ""
        raise ValueError(f"calendar time must be HH:MM{suffix}, got {value!r}")
    hour, minute = text.split(":", 1)
    return int(hour) * 60 + int(minute)


def _parse_utc_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"calendar timestamp must be ISO-8601 with timezone, got {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"calendar timestamp must include timezone, got {value!r}")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class CalendarDayRule:
    day: date
    closed: bool
    open_minute_utc: int = 0
    close_minute_utc: int = 24 * 60
    reason: str = "calendar_day_override"


@dataclass(frozen=True)
class CalendarClosure:
    start_utc: datetime
    end_utc: datetime
    reason: str = "calendar_temporary_closure"


@dataclass(frozen=True)
class MarketCalendar:
    source_path: str
    day_rules: tuple[CalendarDayRule, ...] = ()
    closures: tuple[CalendarClosure, ...] = ()

    def day_rule(self, current: date) -> CalendarDayRule | None:
        for item in self.day_rules:
            if item.day == current:
                return item
        return None


def load_market_calendar(path: str | Path, *, required: bool = False) -> MarketCalendar | None:
    target = Path(path)
    if not target.exists():
        if required:
            raise FileNotFoundError(f"required market calendar not found: {target}")
        return None

    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("market calendar root must be a mapping")
    version = int(raw.get("version", 1))
    if version != 1:
        raise ValueError(f"unsupported market calendar version: {version}")

    raw_days = raw.get("days", {}) or {}
    if not isinstance(raw_days, dict):
        raise ValueError("market calendar days must be a mapping keyed by YYYY-MM-DD")
    day_rules: list[CalendarDayRule] = []
    for key, payload in raw_days.items():
        if not isinstance(payload, dict):
            raise ValueError(f"market calendar day {key!r} must be a mapping")
        day_value = _parse_date(key)
        closed = bool(payload.get("closed", False))
        has_open = "open_utc" in payload
        has_close = "close_utc" in payload
        reason = str(payload.get("reason", "calendar_day_override")).strip() or "calendar_day_override"
        if closed:
            if has_open or has_close:
                raise ValueError(f"closed calendar day {day_value} cannot also define open_utc/close_utc")
            day_rules.append(CalendarDayRule(day=day_value, closed=True, reason=reason))
            continue
        if not has_open and not has_close:
            raise ValueError(f"calendar day {day_value} must set closed=true or define open_utc/close_utc")
        open_minute = _parse_hhmm(payload.get("open_utc", "00:00"))
        close_minute = _parse_hhmm(payload.get("close_utc", "24:00"), allow_2400=True)
        if open_minute >= close_minute:
            raise ValueError(f"calendar day {day_value} requires open_utc < close_utc")
        day_rules.append(
            CalendarDayRule(
                day=day_value,
                closed=False,
                open_minute_utc=open_minute,
                close_minute_utc=close_minute,
                reason=reason,
            )
        )

    raw_closures = raw.get("closures", []) or []
    if not isinstance(raw_closures, list):
        raise ValueError("market calendar closures must be a list")
    closures: list[CalendarClosure] = []
    for payload in raw_closures:
        if not isinstance(payload, dict):
            raise ValueError("each market calendar closure must be a mapping")
        if "start_utc" not in payload or "end_utc" not in payload:
            raise ValueError("market calendar closure requires start_utc and end_utc")
        start = _parse_utc_datetime(payload["start_utc"])
        end = _parse_utc_datetime(payload["end_utc"])
        if end <= start:
            raise ValueError("market calendar closure requires end_utc > start_utc")
        reason = str(payload.get("reason", "calendar_temporary_closure")).strip() or "calendar_temporary_closure"
        closures.append(CalendarClosure(start_utc=start, end_utc=end, reason=reason))

    day_rules.sort(key=lambda item: item.day)
    closures.sort(key=lambda item: (item.start_utc, item.end_utc))
    return MarketCalendar(
        source_path=str(target),
        day_rules=tuple(day_rules),
        closures=tuple(closures),
    )


def _day_start(day_value: date) -> datetime:
    return datetime(day_value.year, day_value.month, day_value.day, tzinfo=timezone.utc)


def _rule_window(rule: CalendarDayRule) -> tuple[datetime, datetime]:
    start = _day_start(rule.day)
    return (
        start + timedelta(minutes=rule.open_minute_utc),
        start + timedelta(minutes=rule.close_minute_utc),
    )


def _calendar_boundaries(calendar: MarketCalendar) -> list[datetime]:
    out: list[datetime] = []
    for rule in calendar.day_rules:
        start = _day_start(rule.day)
        if rule.closed:
            out.extend((start, start + timedelta(days=1)))
        else:
            out.extend(_rule_window(rule))
    for closure in calendar.closures:
        out.extend((closure.start_utc, closure.end_utc))
    return sorted(set(out))


def evaluate_market_calendar(
    now: datetime,
    calendar: MarketCalendar,
    *,
    transition_grace_seconds: float = 0.0,
) -> dict[str, str | bool | None]:
    current = _as_utc(now)
    grace = max(0.0, float(transition_grace_seconds))
    boundaries = _calendar_boundaries(calendar)
    next_boundary = next((stamp for stamp in boundaries if stamp > current), None)

    for closure in calendar.closures:
        if closure.start_utc <= current < closure.end_utc:
            return {
                "state": "CLOSED",
                "reason": closure.reason,
                "next_transition_utc": closure.end_utc.isoformat(),
                "calendar_applied": True,
            }

    rule = calendar.day_rule(current.date())
    if rule is not None and rule.closed:
        return {
            "state": "CLOSED",
            "reason": rule.reason,
            "next_transition_utc": (_day_start(rule.day) + timedelta(days=1)).isoformat(),
            "calendar_applied": True,
        }

    if rule is not None and not rule.closed:
        open_at, close_at = _rule_window(rule)
        if current < open_at:
            if grace > 0 and (open_at - current).total_seconds() <= grace:
                return {
                    "state": "TRANSITION",
                    "reason": f"near_calendar_open:{rule.reason}",
                    "next_transition_utc": open_at.isoformat(),
                    "calendar_applied": True,
                }
            return {
                "state": "CLOSED",
                "reason": f"calendar_delayed_open:{rule.reason}",
                "next_transition_utc": open_at.isoformat(),
                "calendar_applied": True,
            }
        if current >= close_at:
            return {
                "state": "CLOSED",
                "reason": f"calendar_early_close:{rule.reason}",
                "next_transition_utc": next_boundary.isoformat() if next_boundary else None,
                "calendar_applied": True,
            }
        if grace > 0:
            distance_from_open = (current - open_at).total_seconds()
            distance_to_close = (close_at - current).total_seconds()
            if distance_from_open <= grace:
                return {
                    "state": "TRANSITION",
                    "reason": f"near_calendar_open:{rule.reason}",
                    "next_transition_utc": close_at.isoformat(),
                    "calendar_applied": True,
                }
            if distance_to_close <= grace:
                return {
                    "state": "TRANSITION",
                    "reason": f"near_calendar_close:{rule.reason}",
                    "next_transition_utc": close_at.isoformat(),
                    "calendar_applied": True,
                }
        return {
            "state": "OPEN",
            "reason": f"calendar_window_open:{rule.reason}",
            "next_transition_utc": close_at.isoformat(),
            "calendar_applied": True,
        }

    if grace > 0:
        for closure in calendar.closures:
            if current < closure.start_utc and (closure.start_utc - current).total_seconds() <= grace:
                return {
                    "state": "TRANSITION",
                    "reason": f"near_calendar_closure:{closure.reason}",
                    "next_transition_utc": closure.start_utc.isoformat(),
                    "calendar_applied": True,
                }
            if current >= closure.end_utc and (current - closure.end_utc).total_seconds() <= grace:
                return {
                    "state": "TRANSITION",
                    "reason": f"after_calendar_closure:{closure.reason}",
                    "next_transition_utc": next_boundary.isoformat() if next_boundary else None,
                    "calendar_applied": True,
                }
        for item in calendar.day_rules:
            if item.closed:
                start = _day_start(item.day)
                end = start + timedelta(days=1)
                if current < start and (start - current).total_seconds() <= grace:
                    return {
                        "state": "TRANSITION",
                        "reason": f"near_calendar_day_close:{item.reason}",
                        "next_transition_utc": start.isoformat(),
                        "calendar_applied": True,
                    }
                if current >= end and (current - end).total_seconds() <= grace:
                    return {
                        "state": "TRANSITION",
                        "reason": f"after_calendar_day_close:{item.reason}",
                        "next_transition_utc": next_boundary.isoformat() if next_boundary else None,
                        "calendar_applied": True,
                    }

    return {
        "state": "OPEN",
        "reason": "calendar_no_override",
        "next_transition_utc": next_boundary.isoformat() if next_boundary else None,
        "calendar_applied": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and inspect the Phase 24 market calendar")
    parser.add_argument("--path", default="market_calendar.yaml")
    parser.add_argument("--at", help="ISO-8601 UTC timestamp to inspect")
    args = parser.parse_args()
    calendar = load_market_calendar(args.path, required=True)
    assert calendar is not None
    payload: dict[str, Any] = {
        "ok": True,
        "path": calendar.source_path,
        "day_rules": len(calendar.day_rules),
        "closures": len(calendar.closures),
    }
    if args.at:
        when = _parse_utc_datetime(args.at)
        payload["state"] = evaluate_market_calendar(when, calendar)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
