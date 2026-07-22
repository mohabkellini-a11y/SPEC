"""Polite HTTP client: per-host rate limiting, jitter, honest User-Agent, retries.

Every adapter shares this so the respectful-collection rules in SPEC §2 are
enforced in one place instead of per-adapter.
"""

from __future__ import annotations

import random
import time
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

# An honest, descriptive UA with a contact email (SPEC §2).
USER_AGENT = (
    "EverSafeLeads/0.1 (Central FL fire-protection lead research; contact: info@eversafe-fire.com)"
)


class RateLimiter:
    """One request per ``min_interval`` seconds per host, plus jitter."""

    def __init__(self, min_interval: float = 2.0, jitter: float = 1.0) -> None:
        self.min_interval = min_interval
        self.jitter = jitter
        self._last: dict[str, float] = {}

    def wait(self, host: str) -> None:
        now = time.monotonic()
        last = self._last.get(host)
        if last is not None:
            elapsed = now - last
            target = self.min_interval + random.uniform(0, self.jitter)
            if elapsed < target:
                time.sleep(target - elapsed)
        self._last[host] = time.monotonic()


class PoliteClient:
    """Thin wrapper over ``httpx.Client`` that rate-limits and retries."""

    def __init__(
        self,
        min_interval: float = 2.0,
        jitter: float = 1.0,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.limiter = RateLimiter(min_interval, jitter)
        base_headers = {"User-Agent": USER_AGENT}
        if headers:
            base_headers.update(headers)
        self._client = httpx.Client(timeout=timeout, headers=base_headers, follow_redirects=True)

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=16),
        reraise=True,
    )
    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        host = httpx.URL(url).host or ""
        self.limiter.wait(host)
        resp = self._client.get(url, **kwargs)
        resp.raise_for_status()
        return resp

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.get(url, **kwargs).json()

    def stream(self, method: str, url: str, **kwargs: Any):
        """Rate-limited streaming request (context manager), for large files."""
        host = httpx.URL(url).host or ""
        self.limiter.wait(host)
        return self._client.stream(method, url, **kwargs)
