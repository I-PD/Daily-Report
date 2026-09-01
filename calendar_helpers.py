import os
from datetime import datetime, date, time as dt_time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Lisbon")

def parse_holidays() -> set[date]:
    raw = os.getenv("HOLIDAYS", "")
    holidays: set[date] = set()

    for item in raw.split(","):
        item = item.strip()

        if not item:
            continue

        try:
            holidays.add(date.fromisoformat(item))
        except ValueError as exc:
            raise ValueError(
                f"HOLIDAYS contém uma data inválida: {item!r}. "
                "Usar formato YYYY-MM-DD."
            ) from exc

    return holidays


def is_holiday(day: date) -> bool:
    return day in parse_holidays()


def is_weekend(day: date) -> bool:
    return day.weekday() >= 5


def is_operational_date(day: date) -> bool:
    """
    Mantido para classificação/calendário.

    Um dia operacional normal é:
    - segunda a sexta;
    - não incluído em HOLIDAYS.
    """
    return not is_weekend(day) and not is_holiday(day)

# def parse_holidays():
#     raw = os.getenv("HOLIDAYS", "")
#     holidays = set()

#     for item in raw.split(","):
#         item = item.strip()
#         if item:
#             holidays.add(date.fromisoformat(item))

#     return holidays

# def is_operational_date(day: date) -> bool:
#     """
#     Um dia operacional normal é:
#     - segunda a sexta;
#     - não incluído em HOLIDAYS.

#     Um sábado, domingo ou feriado ainda poderá originar
#     relatório se existir produção.
#     """
#     if day.weekday() >= 5:
#         return False

#     if day in parse_holidays():
#         return False

#     return True

def is_operational_day(now: datetime) -> bool:
    return is_operational_date(now.date())

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
        candidate_dt = datetime.combine(
            candidate, 
            dt_time(0, 0), 
            tzinfo=TZ
        )

        if is_operational_day(candidate_dt):
            return candidate

        candidate -= timedelta(days=1)