"""Memória de curto prazo (working memory) da sessão atual.

Vive em RAM e expira sozinha. Não é RAG e não é histórica: representa apenas
"o que está acontecendo agora" nesta sessão. A presença de pessoas deve vir da
percepção visual atual, não de memórias de reconhecimento antigas.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .timeutil import now_local, to_iso

DEFAULT_TTL_SEC = 1800
DEFAULT_PEOPLE_TTL_SEC = 120


@dataclass
class Turn:
    role: str
    text: str
    at: datetime


@dataclass
class WorkingMemory:
    ttl_seconds: int = DEFAULT_TTL_SEC
    max_turns: int = 16
    people_ttl_seconds: int = DEFAULT_PEOPLE_TTL_SEC

    _turns: deque[Turn] = field(default_factory=lambda: deque(maxlen=16))
    _state: dict[str, tuple[Any, Optional[float]]] = field(default_factory=dict)
    _people: list[str] = field(default_factory=list)
    _people_at: float = 0.0
    _started_at: datetime = field(default_factory=now_local)
    _started_mono: float = field(default_factory=time.monotonic)
    _last_access: float = field(default_factory=time.monotonic)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        self._turns = deque(maxlen=max(2, int(self.max_turns)))

    def _now_monotonic(self) -> float:
        return time.monotonic()

    def _prune(self, current: Optional[float] = None) -> None:
        current = current if current is not None else self._now_monotonic()
        if current - self._last_access > max(1, int(self.ttl_seconds)):
            self._turns.clear()
            self._state.clear()
            self._people = []
            self._people_at = 0.0
            self._started_at = now_local()
            self._started_mono = current
        self._last_access = current

        # Estado com TTL individual.
        for key, (_, expires) in list(self._state.items()):
            if expires is not None and expires <= current:
                del self._state[key]

    def reset(self) -> None:
        with self._lock:
            self._turns.clear()
            self._state.clear()
            self._people = []
            self._people_at = 0.0
            self._started_at = now_local()
            self._started_mono = self._now_monotonic()
            self._last_access = self._now_monotonic()

    def add_turn(self, role: str, text: str, *, at: Optional[datetime] = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._prune()
            self._turns.append(Turn(role=role, text=text, at=at or now_local()))

    def set_state(self, key: str, value: Any, *, ttl_seconds: Optional[float] = None) -> None:
        with self._lock:
            self._prune()
            expires = (
                self._now_monotonic() + float(ttl_seconds)
                if ttl_seconds and ttl_seconds > 0
                else None
            )
            if value is None:
                self._state.pop(key, None)
            else:
                self._state[key] = (value, expires)

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._lock:
            self._prune()
            item = self._state.get(key)
            return item[0] if item is not None else default

    def set_people(self, names: list[str], *, at: Optional[datetime] = None) -> None:
        cleaned: list[str] = []
        for name in names or []:
            name = str(name or "").strip()
            if name and name not in cleaned:
                cleaned.append(name)
        with self._lock:
            self._prune()
            self._people = cleaned
            self._people_at = self._now_monotonic()

    def current_people(self) -> list[str]:
        with self._lock:
            self._prune()
            if not self._people:
                return []
            if self._now_monotonic() - self._people_at > max(1, int(self.people_ttl_seconds)):
                return []
            return list(self._people)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._prune()
            current = self._now_monotonic()
            state = {key: value for key, (value, _) in self._state.items()}
            people = list(self._people)
            if people and current - self._people_at > max(1, int(self.people_ttl_seconds)):
                people = []
            return {
                "started_at": to_iso(self._started_at),
                "age_seconds": int(max(0.0, current - self._started_mono)),
                "turns": [
                    {
                        "role": turn.role,
                        "text": turn.text,
                        "at": to_iso(turn.at),
                    }
                    for turn in list(self._turns)
                ],
                "people": people,
                "state": state,
                "active_task": state.get("active_task", ""),
            }
