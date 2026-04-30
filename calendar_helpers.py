import os
from datetime import datetime, date, time as dt_time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Lisbon")


def parse_holidays():
    raw = os.getenv("HOLIDAYS", "")
    holidays = set()

    for item in raw.split(","):
        item = item.strip()
        if item:
            holidays.add(date.fromisoformat(item))

    return holidays


def is_operational_day(now):
    # sábado = 5, domingo = 6
    if now.weekday() >= 5:
        return False

    if now.date() in parse_holidays():
        return False

    return True


def seconds_until_next_operational_day(now):
    next_day = now.date() + timedelta(days=1)

    while True:
        candidate = datetime.combine(next_day, dt_time(0, 0), tzinfo=TZ)

        if is_operational_day(candidate):
            return max(60, int((candidate - now).total_seconds()))

        next_day += timedelta(days=1)

def previous_operational_day(now):
    """
    Devolve a data do último dia operacional anterior.

    Exemplo:
    - se hoje é terça e segunda foi feriado, devolve sexta
    - se hoje é segunda, devolve sexta
    - se sexta foi feriado, continua a procurar para trás
    """
    candidate = now.date() - timedelta(days=1)

    while True:
        candidate_dt = datetime.combine(candidate, dt_time(0, 0), tzinfo=TZ)

        if is_operational_day(candidate_dt):
            return candidate

        candidate -= timedelta(days=1)