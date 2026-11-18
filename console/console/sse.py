"""Server-sent events: a small in-process broker and the wire format.

``Broker`` hands each event published on a topic to every subscriber of that topic. An event is encoded once, at
``publish``, into a ``Frame`` (the id and the exact bytes of the ``text/event-stream`` message); every subscriber
gets the same immutable frame, so the publisher may change its data afterwards and twenty streams cost one
encoding. An event may also be encoded once more for the admin (``admin``: what the facilitator may do differs, as
the answers to a run's question do); a stream sends that one to the admin. A subscriber is a bounded ``queue.Queue``
of frames; one that falls ``max_queue`` events behind is dropped (its ``dropped`` flag is set and it receives nothing
more), and the browser's ``EventSource`` reconnects and asks again.

``write_sse`` writes one event in that format; ``stream`` is the loop an HTTP handler runs over a subscriber's queue:
frames as they come, a comment line (``: hb``) after ``heartbeat_s`` of quiet so that a vanished client is noticed
by the failing write, and an end when the subscriber was dropped (or, for a stream that ends, when it is done). A run
stream also drops a frame whose id is not above the last id it sent. ``StreamLimit`` caps the streams one client holds.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from typing import BinaryIO, Callable, NamedTuple

__all__ = ["HEARTBEAT_S", "MAX_QUEUE", "POLL_S", "Broker", "Frame", "StreamLimit", "Subscriber", "stream", "write_sse"]

HEARTBEAT_S = 15.0
MAX_QUEUE = 1000
POLL_S = 1.0  # how often a stream that ends asks whether it is done

_EVENT_NAME = re.compile(r"[A-Za-z0-9._-]+")
_HEARTBEAT = b": hb\n\n"


class Frame(NamedTuple):
    """One event ready to send: its SSE ``id`` (``None`` if it has none; a run stream de-duplicates by it), the bytes,
    and ``admin``, the admin's bytes when they differ."""

    id: int | None
    data: bytes
    admin: bytes | None = None


class Subscriber(queue.Queue):
    """A subscriber's queue of ``Frame``. ``dropped`` is set by the broker when it gave up on it."""

    def __init__(self, maxsize: int) -> None:
        super().__init__(maxsize)
        self.dropped = False


class Broker:
    """Publish/subscribe by topic. Safe to use from many threads."""

    def __init__(self, heartbeat_s: float = HEARTBEAT_S, max_queue: int = MAX_QUEUE) -> None:
        self.heartbeat_s = heartbeat_s  # read by each stream when it starts; tests make it short
        self._max_queue = max_queue
        self._topics: dict[str, list[Subscriber]] = {}
        self._lock = threading.Lock()

    def subscribe(self, topic: str) -> Subscriber:
        sub = Subscriber(self._max_queue)
        with self._lock:
            self._topics.setdefault(topic, []).append(sub)
        return sub

    def unsubscribe(self, topic: str, sub: queue.Queue) -> None:
        """Remove ``sub`` from ``topic``; does nothing when it is not there (a dropped one already is)."""
        with self._lock:
            subs = self._topics.get(topic)
            if subs is not None and sub in subs:
                subs.remove(sub)
                if not subs:
                    del self._topics[topic]

    def publish(self, topic: str, event: str, data: dict, id: int | None = None, *, admin: dict | None = None) -> None:
        """Encode the event once (and ``admin``, the admin's data, once more) and queue it for every subscriber of
        ``topic``; a full queue is dropped.

        ``ValueError`` (here, not in a stream) for an event name or data that cannot be sent.
        """
        item = Frame(id, _frame(event, data, id), None if admin is None else _frame(event, admin, id))
        with self._lock:
            for sub in list(self._topics.get(topic, ())):
                try:
                    sub.put_nowait(item)
                except queue.Full:
                    sub.dropped = True
                    self._topics[topic].remove(sub)
            if topic in self._topics and not self._topics[topic]:
                del self._topics[topic]

    def subscriber_count(self, topic: str) -> int:
        with self._lock:
            return len(self._topics.get(topic, ()))


def _frame(event: str, data: dict, id: int | None) -> bytes:
    if not isinstance(event, str) or not _EVENT_NAME.fullmatch(event):
        raise ValueError(f"event name must be letters, digits, '.', '_' or '-', got {event!r}")
    try:
        text = json.dumps(data, separators=(",", ":"), allow_nan=False)  # one line: JSON escapes newlines
    except TypeError as err:
        raise ValueError(f"data is not JSON: {err}") from None
    head = f"id: {id}\n" if id is not None else ""
    return f"{head}event: {event}\ndata: {text}\n\n".encode("utf-8")


def write_sse(wfile: BinaryIO, event: str, data: dict, id: int | None = None) -> None:
    """Write one event to ``wfile``. ``OSError`` (broken pipe, reset) when the client is gone."""
    wfile.write(_frame(event, data, id))
    wfile.flush()


def stream(
    wfile: BinaryIO,
    sub: Subscriber,
    heartbeat_s: float,
    *,
    after: int | None = None,
    done: Callable[[], bool] | None = None,
    poll_s: float = POLL_S,
    admin: bool = False,
) -> None:
    """Send ``sub``'s events to ``wfile`` until it is dropped, or ``done``. ``OSError`` when the client is gone.

    ``after`` makes the ids only grow (a run stream): a frame whose id is at or below ``after``, or at or below an id
    sent already, is dropped; frames without an id always go. ``done`` is asked at once and then at most every
    ``poll_s``; once it says True, the frames still queued are sent and the call returns. ``admin``: the stream is
    the admin's (a frame's admin bytes go out where it has them).
    """
    wait = heartbeat_s if done is None else min(poll_s, heartbeat_s)
    quiet, next_ask = 0.0, time.monotonic()
    while not sub.dropped:
        if done is not None and time.monotonic() >= next_ask:
            if done():  # what was published before it said so is queued already
                while True:
                    try:
                        frame = sub.get_nowait()
                    except queue.Empty:
                        return
                    after = _send(wfile, frame, after, admin)
            next_ask = time.monotonic() + poll_s
        try:
            frame = sub.get(timeout=wait)
        except queue.Empty:
            quiet += wait
            if quiet >= heartbeat_s:
                quiet = 0.0
                wfile.write(_HEARTBEAT)
                wfile.flush()
            continue
        quiet = 0.0
        after = _send(wfile, frame, after, admin)


def _send(wfile: BinaryIO, frame: Frame, after: int | None, admin: bool) -> int | None:
    """Write ``frame`` (its admin bytes for the admin) unless its id is not above ``after``; returns the highest id
    sent."""
    if after is not None and frame.id is not None:
        if frame.id <= after:
            return after
        after = frame.id
    wfile.write(frame.admin if admin and frame.admin is not None else frame.data)
    wfile.flush()
    return after


class StreamLimit:
    """At most ``limit`` open streams per client (``key`` is an address). Thread-safe; ``limit`` may be changed."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._open: dict[str, int] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str) -> bool:
        """Count one more stream for ``key``; ``False`` (nothing counted) when it holds ``limit`` already."""
        with self._lock:
            held = self._open.get(key, 0)
            if held >= self.limit:
                return False
            self._open[key] = held + 1
            return True

    def release(self, key: str) -> None:
        with self._lock:
            held = self._open.get(key, 0)
            if held <= 1:
                self._open.pop(key, None)
            else:
                self._open[key] = held - 1
