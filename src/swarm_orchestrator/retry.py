"""Exponential backoff with equal jitter, plus Retry-After handling."""

from __future__ import annotations

import random


class RetryableError(Exception):
    """A task attempt failed and may be tried again."""


class RateLimitError(RetryableError):
    def __init__(self, message: str = "rate limited", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class TaskTimeout(RetryableError):
    pass


class CrashBeforeAck(Exception):
    """The attempt ended before the broker message was acknowledged."""


def backoff_seconds(attempt: int, base: float, cap: float, rng: random.Random) -> float:
    """Equal jitter: half the exponential delay, plus a uniform half.

    ``attempt`` is 1-based. The delay grows as ``base * 2^(attempt-1)`` and
    never exceeds ``cap`` before jitter is applied inside that window.
    """

    if base < 0 or cap < 0:
        raise ValueError("backoff bounds must be non-negative")
    exponent = max(0, attempt - 1)
    exponential = min(cap, base * (2**exponent))
    if exponential == 0:
        return 0.0
    half = exponential / 2.0
    return half + rng.uniform(0.0, half)


def retry_delay(
    attempt: int,
    base: float,
    cap: float,
    rng: random.Random,
    retry_after: float | None = None,
) -> float:
    """Backoff, raised to a server Retry-After when one is provided."""

    delay = backoff_seconds(attempt, base, cap, rng)
    if retry_after is None:
        return delay
    if retry_after < 0:
        raise ValueError("retry_after must be non-negative")
    return max(delay, retry_after)
