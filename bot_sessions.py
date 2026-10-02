"""Bounded single-process FSM state and per-session event isolation."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import math
import time

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage


class SessionCapacityError(RuntimeError):
    pass


@dataclass
class Session:
    touched: float
    state: str | None = None
    data: dict = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class BotSessions(BaseStorage, BaseEventIsolation):
    def __init__(self, *, capacity=1000, ttl=1800, clock=time.monotonic):
        if type(capacity) is not int or not 1 <= capacity <= 100_000:
            raise ValueError("capacity must be an integer in [1, 100000]")
        if type(ttl) not in (int, float) or not math.isfinite(ttl) or not 1 <= ttl <= 86400:
            raise ValueError("ttl must be a finite number in [1, 86400]")
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self.sessions = {}
        self.closed = False

    def _session(self, key):
        if self.closed:
            raise RuntimeError("Session storage is closed")
        now = self.clock()
        for old_key, old in list(self.sessions.items()):
            if old.users == 0 and (now - old.touched >= self.ttl
                                   or (old.state is None and not old.data)):
                del self.sessions[old_key]
        if key not in self.sessions:
            if len(self.sessions) >= self.capacity:
                raise SessionCapacityError("Session capacity reached")
            self.sessions[key] = Session(touched=now)
        return self.sessions[key]

    @asynccontextmanager
    async def lock(self, key):
        session = self._session(key)
        session.users += 1
        try:
            async with session.lock:
                yield
        finally:
            session.users -= 1
            session.touched = self.clock()
            if session.users == 0 and session.state is None and not session.data:
                self.sessions.pop(key, None)

    async def set_state(self, key, state=None):
        self._session(key).state = state.state if isinstance(state, State) else state

    async def get_state(self, key):
        return self._session(key).state

    async def set_data(self, key, data):
        if not isinstance(data, dict):
            raise TypeError("FSM data must be a dict")
        self._session(key).data = deepcopy(data)

    async def get_data(self, key):
        return deepcopy(self._session(key).data)

    async def close(self):
        if any(session.users for session in self.sessions.values()):
            raise RuntimeError("Drain update handlers before closing session storage")
        self.closed = True
        self.sessions.clear()
