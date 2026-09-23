import asyncio
import logging
from collections import deque

from ananta.core.events import Event

logger = logging.getLogger(__name__)


class EventQueue:
    def __init__(self) -> None:
        self._queue: deque[Event] = deque()
        self._lock: asyncio.Lock | None = None
        self._event: asyncio.Event | None = None

    def _ensure_async_objects(self) -> None:
        """Lazy initialization of async objects when needed"""
        if self._lock is None:
            try:
                self._lock = asyncio.Lock()
                self._event = asyncio.Event()
            except Exception:
                raise

    async def enqueue(self, event: Event) -> None:
        self._ensure_async_objects()
        assert (
            self._lock is not None and self._event is not None
        )  # Ensured by _ensure_async_objects
        async with self._lock:
            self._queue.append(event)
        self._event.set()  # Wake up dequeue waiter

    async def enqueue_many(self, events: list[Event]) -> None:
        if not events:
            return
        self._ensure_async_objects()
        assert (
            self._lock is not None and self._event is not None
        )  # Ensured by _ensure_async_objects
        async with self._lock:
            for event in events:
                self._queue.append(event)
        self._event.set()

    async def dequeue(self, timeout: float | None = None) -> Event | None:
        self._ensure_async_objects()
        assert (
            self._lock is not None and self._event is not None
        )  # Ensured by _ensure_async_objects
        try:
            # Wait for events to be available
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except TimeoutError:
            return None

        async with self._lock:
            if self._queue:
                event = self._queue.popleft()

                # Reset event if queue is empty
                if not self._queue:
                    self._event.clear()

                return event
            else:
                # Queue became empty between wait and lock
                self._event.clear()
                return None

    def size(self) -> int:
        return len(self._queue)
