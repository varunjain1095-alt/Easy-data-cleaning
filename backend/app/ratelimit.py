"""In-process sliding-window rate limiting for upload and processing endpoints.

Keyed on session token (falling back to client host). Version 1 runs a single
process, so in-memory counters are sufficient; no Redis required.
"""

import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# path prefix -> (max requests, window seconds)
RULES = [
    ("/api/upload", 10, 60),
    ("/api/items/", 120, 60),          # ops/preview/assess/treat etc.
    ("/api/export", 12, 60),
]


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, rules=None):
        super().__init__(app)
        self.rules = rules or RULES
        self._hits: dict[tuple[str, str], deque] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        if request.method != "POST":
            return await call_next(request)
        path = request.url.path
        limit = window = None
        for prefix, m, w in self.rules:
            if path.startswith(prefix):
                limit, window = m, w
                break
        if limit is None:
            return await call_next(request)

        key = (
            request.headers.get("x-session-token")
            or (request.client.host if request.client else "unknown"),
            path.split("/")[3] if path.startswith("/api/items/") else path,
        )
        now = time.monotonic()
        dq = self._hits[key]
        while dq and dq[0] < now - window:
            dq.popleft()
        if len(dq) >= limit:
            return JSONResponse(
                {"detail": {"kind": "rate_limited", "detail": "Too many requests; slow down."}},
                status_code=429,
            )
        dq.append(now)
        return await call_next(request)
