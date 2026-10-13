"""Ограниченный сбор метаданных WebSocket-событий."""

from __future__ import annotations

from collections import deque
from typing import Any


class WebSocketMonitor:
    def __init__(self, max_events: int = 100):
        self.max_events = max(1, min(int(max_events), 1000))
        self.events = deque(maxlen=self.max_events)
        self._attached: set[int] = set()

    def attach(self, page: Any) -> None:
        key = id(page)
        if key in self._attached:
            return
        self._attached.add(key)

        def on_socket(ws):
            url = str(getattr(ws, "url", ""))[:1000]
            self.events.append({"event": "opened", "url": url})

            def frame_handler(event_name):
                def callback(payload):
                    try:
                        size = len(payload)
                    except Exception:
                        size = None
                    self.events.append({
                        "event": event_name,
                        "url": url,
                        "payload_size": size,
                    })
                return callback

            for event_name in ("framesent", "framereceived"):
                try:
                    ws.on(event_name, frame_handler(event_name))
                except Exception:
                    pass

            try:
                ws.on("close", lambda: self.events.append({
                    "event": "closed", "url": url
                }))
            except Exception:
                pass

            try:
                ws.on("socketerror", lambda error: self.events.append({
                    "event": "error",
                    "url": url,
                    "error": str(error)[:300],
                }))
            except Exception:
                pass

        page.on("websocket", on_socket)

    def snapshot(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), self.max_events))
        return list(self.events)[-limit:]