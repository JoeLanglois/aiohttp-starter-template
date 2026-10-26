"""A tiny in-process pub/sub bus — the backbone of server-push updates.

Publishers just name a topic ("something changed for user 3"); they
don't know or care who is listening. Subscribers get an asyncio.Queue
that is *nudged* on every publish. Messages carry no payload on
purpose: a subscriber's job is to re-read current state and re-render,
so only the latest state matters. Queues are therefore size-1 and
coalescing — publishing to a full queue replaces the pending nudge
instead of piling up stale ones.

This is deliberately single-process and in-memory, like Hyperlith: for
a personal app on one box, a real message broker is ceremony. If you
ever run multiple workers, this class is the seam to swap (Redis
pub/sub has the same shape).

    queue = bus.subscribe(topic)   # in the SSE handler
    bus.unsubscribe(topic, queue)  # in a finally — always
    bus.publish(topic)             # after any mutation of that state
"""

import asyncio
from collections import defaultdict


class Bus:
    def __init__(self) -> None:
        self._topics: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, topic: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._topics[topic].add(queue)
        return queue

    def unsubscribe(self, topic: str, queue: asyncio.Queue) -> None:
        subs = self._topics.get(topic)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                del self._topics[topic]

    def publish(self, topic: str) -> None:
        for queue in list(self._topics.get(topic, ())):
            if queue.full():
                queue.get_nowait()  # drop the stale nudge; latest wins
            queue.put_nowait(None)

    def subscriber_count(self, topic: str) -> int:
        return len(self._topics.get(topic, ()))
