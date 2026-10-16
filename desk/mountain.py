"""Mountain Time for what SHADOW and LIVE show you: the SHADOW page's text, the phone pushes, the run's own log. The
owner reads times in America/Denver's zone (MDT in summer, MST in winter: the zone's own rules, never a fixed offset),
written like "10:08 am MT" or "Oct 15, 10:08 am MT". Only what's shown: the logic, the 6-hour push windows and every
stored timestamp stay UTC epoch ms. The zone comes from the system's tz database (zoneinfo; GitHub's runners have it); a
machine without it shows UTC, labelled so, never a wrong time called MT."""

from __future__ import annotations

from datetime import datetime, timezone

ZONE = "America/Denver"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _zone():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(ZONE)
    except Exception:           # noqa: BLE001 (no tz database here: UTC, said as UTC)
        return None


TZ = _zone()


def local(ms: int) -> tuple[datetime, str]:
    """(the moment in Mountain Time, "MT"), or in UTC with "UTC" where the zone isn't known."""
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return (d.astimezone(TZ), "MT") if TZ is not None else (d, "UTC")


def clock(ms: int, seconds: bool = False, label: bool = True) -> str:
    """"10:08 am MT" ("10:08:03 am" with seconds and no label)."""
    d, zone = local(ms)
    t = f"{d.hour % 12 or 12}:{d.minute:02d}" + (f":{d.second:02d}" if seconds else "") + \
        (" am" if d.hour < 12 else " pm")
    return f"{t} {zone}" if label else t


def day(ms: int) -> str:
    """"Oct 15": the date in Mountain Time."""
    d, _ = local(ms)
    return f"{MONTHS[d.month - 1]} {d.day}"


def when(ms: int) -> str:
    """"Oct 15, 10:08 am MT"."""
    return f"{day(ms)}, {clock(ms)}"
