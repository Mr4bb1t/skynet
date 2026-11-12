"""Utilitários de tempo da memória (fuso America/Sao_Paulo).

Centraliza:
  * obtenção do "agora" local;
  * parsing de datas relativas ("hoje", "ontem", "há 3 dias");
  * formatação absoluta + idade relativa;
  * faixas temporais usadas na recuperação.

Regra de ouro: `created_at` é quando a memória foi SALVA; `event_time` é quando
o acontecimento OCORREU. Este módulo nunca usa um como o outro.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python mínimo
    ZoneInfo = None  # type: ignore

TZ_NAME = os.getenv("SKYNET_TIMEZONE", "America/Sao_Paulo")


def local_tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(TZ_NAME)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def now_local() -> datetime:
    return datetime.now(local_tz())


def to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=local_tz())
    return value.isoformat()


def parse_iso(value: str, *, assume_tz=None) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=assume_tz or local_tz())
    return parsed


def _date_at_start(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=local_tz())


def _parse_relative(text: str, now: datetime) -> Optional[datetime]:
    lowered = text.strip().lower()
    today = now.date()

    exact = {
        "agora": now,
        "agora mesmo": now,
        "neste momento": now,
        "hoje": _date_at_start(today),
        "ontem": _date_at_start(today - timedelta(days=1)),
        "anteontem": _date_at_start(today - timedelta(days=2)),
        "amanhã": _date_at_start(today + timedelta(days=1)),
        "amanha": _date_at_start(today + timedelta(days=1)),
        "depois de amanhã": _date_at_start(today + timedelta(days=2)),
        "depois de amanha": _date_at_start(today + timedelta(days=2)),
        "semana passada": _date_at_start(today - timedelta(days=7)),
        "mês passado": _date_at_start(today - timedelta(days=30)),
        "mes passado": _date_at_start(today - timedelta(days=30)),
    }
    if lowered in exact:
        return exact[lowered]

    # "há 3 dias", "3 dias atrás", "há 2 semanas", "há 4 horas"
    match = re.search(
        r"(?:h[áa]\s*)?(\d+)\s*(minuto|minutos|hora|horas|dia|dias|semana|semanas|m[êe]s|meses|ano|anos)"
        r"(?:\s*atr[áa]s)?",
        lowered,
    )
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if unit.startswith("minuto"):
            return now - timedelta(minutes=amount)
        if unit.startswith("hora"):
            return now - timedelta(hours=amount)
        if unit.startswith("dia"):
            return _date_at_start(today - timedelta(days=amount))
        if unit.startswith("semana"):
            return _date_at_start(today - timedelta(weeks=amount))
        if unit.startswith("m"):
            return _date_at_start(today - timedelta(days=30 * amount))
        if unit.startswith("ano"):
            return _date_at_start(today - timedelta(days=365 * amount))

    # Nome do dia da semana -> ocorrência mais recente (inclui hoje).
    weekdays = {
        "segunda": 0, "segunda-feira": 0,
        "terça": 1, "terca": 1, "terça-feira": 1, "terca-feira": 1,
        "quarta": 2, "quarta-feira": 2,
        "quinta": 3, "quinta-feira": 3,
        "sexta": 4, "sexta-feira": 4,
        "sábado": 5, "sabado": 5,
        "domingo": 6,
    }
    if lowered in weekdays:
        target = weekdays[lowered]
        delta = (today.weekday() - target) % 7
        return _date_at_start(today - timedelta(days=delta))

    return None


def parse_when(value: str, *, now: Optional[datetime] = None) -> str:
    """Normaliza uma data/hora para ISO local. Retorna '' se não reconhecer.

    Não inventa horário: datas sem hora viram 00:00 local. Valores ambíguos
    retornam '' para que o chamador marque `temporal_scope='unknown'`.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    current = now or now_local()

    # ISO completo, ISO com Z, ou date-only.
    normalized = text.replace("Z", "+00:00")
    parsed = parse_iso(normalized)
    if parsed is not None:
        return to_iso(parsed)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        try:
            day = date.fromisoformat(normalized)
        except ValueError:
            return ""
        return to_iso(_date_at_start(day))

    relative = _parse_relative(text, current)
    if relative is not None:
        return to_iso(relative)

    # Formatos brasileiros dd/mm/aaaa e dd/mm/aaaa hh:mm.
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(normalized, fmt).replace(tzinfo=local_tz())
            return to_iso(parsed)
        except ValueError:
            continue
    return ""


def relative_age(value: str, *, now: Optional[datetime] = None) -> str:
    parsed = parse_iso(value)
    if parsed is None:
        return ""
    current = now or now_local()
    local = parsed.astimezone(local_tz())
    days = (local.date() - current.astimezone(local_tz()).date()).days
    if days == 0:
        return "hoje"
    if days == 1:
        return "ontem"
    if days == -1:
        return "amanhã"
    if days > 1:
        return f"há {days} dias"
    return f"em {abs(days)} dias"


def format_when(value: str, *, now: Optional[datetime] = None) -> str:
    """Data absoluta + idade relativa. Vazio -> 'data desconhecida'."""
    parsed = parse_iso(value)
    if parsed is None:
        return "data desconhecida"
    local = parsed.astimezone(local_tz())
    return f"{local:%d/%m/%Y} ({relative_age(value, now=now)})"


def format_now(now: Optional[datetime] = None) -> str:
    current = (now or now_local()).astimezone(local_tz())
    return f"{current:%d/%m/%Y %H:%M} {current:%Z}"


def temporal_range(intent: str, *, now: Optional[datetime] = None) -> Optional[Tuple[str, str]]:
    """Faixa (início, fim) para intenções temporais de recuperação."""
    current = now or now_local()
    today = current.date()
    if intent == "today":
        start, end = today, today + timedelta(days=1)
    elif intent == "yesterday":
        start, end = today - timedelta(days=1), today
    elif intent == "week":
        start, end = today - timedelta(days=7), today + timedelta(days=1)
    elif intent == "last_week":
        start, end = today - timedelta(days=14), today - timedelta(days=7)
    elif intent == "month":
        start, end = today - timedelta(days=30), today + timedelta(days=1)
    elif intent == "last_month":
        start, end = today - timedelta(days=60), today - timedelta(days=30)
    elif intent == "future":
        return to_iso(current), ""
    elif intent == "past":
        return "", to_iso(current)
    else:
        return None
    return to_iso(_date_at_start(start)), to_iso(_date_at_start(end))


_NOW_WORDS = (
    "agora",
    "atualmente",
    "neste momento",
    "neste instante",
    "no momento",
    "presentemente",
    "o que está acontecendo",
    "o que esta acontecendo",
)


def detect_temporal_intent(text: str) -> str:
    """Classifica a intenção temporal de uma consulta livre."""
    lowered = (text or "").lower()
    if any(word in lowered for word in _NOW_WORDS):
        return "now"
    if "depois de amanhã" in lowered or "depois de amanha" in lowered:
        return "future"
    if "amanhã" in lowered or "amanha" in lowered:
        return "future"
    if "ontem" in lowered:
        return "yesterday"
    if "anteontem" in lowered:
        return "yesterday"
    if "hoje" in lowered:
        return "today"
    if "semana passada" in lowered:
        return "last_week"
    if "esta semana" in lowered or "nesta semana" in lowered:
        return "week"
    if "mês passado" in lowered or "mes passado" in lowered:
        return "last_month"
    if "este mês" in lowered or "neste mês" in lowered or "este mes" in lowered:
        return "month"
    if any(word in lowered for word in ("vai acontecer", "vou ", "planejo", "plano", "futuro", "amanhã")):
        return "future"
    if any(word in lowered for word in ("aconteceu", "passado", "lembra", "histórico", "historico")):
        return "past"
    return "any"
