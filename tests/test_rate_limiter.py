"""Tests for RateLimiter thread safety."""
import threading
import time

from scrapers.utils import RateLimiter


def test_rate_limiter_sequential_timing():
    """Two consecutive waits on the same domain should respect delay."""
    rl = RateLimiter(delay_seconds=0.2)
    start = time.time()
    rl.wait("example.com")
    rl.wait("example.com")
    elapsed = time.time() - start
    assert elapsed >= 0.18, f"Expected >= 0.18s, got {elapsed:.3f}s"


def test_rate_limiter_different_domains():
    """Different domains should not block each other."""
    rl = RateLimiter(delay_seconds=0.5)
    start = time.time()
    rl.wait("a.com")
    rl.wait("b.com")
    elapsed = time.time() - start
    # Should be fast — no delay between different domains
    assert elapsed < 0.4, f"Different domains should not block; elapsed {elapsed:.3f}s"


def test_rate_limiter_thread_safety():
    """5 threads hitting the same domain — verify no race condition."""
    rl = RateLimiter(delay_seconds=0.05)
    call_times = []
    lock = threading.Lock()

    def worker():
        rl.wait("shared.com")
        with lock:
            call_times.append(time.time())

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    # All 5 threads should have completed
    assert len(call_times) == 5
    # Calls should be sequential (each at least 0.04s apart due to delay)
    call_times.sort()
    for i in range(1, len(call_times)):
        gap = call_times[i] - call_times[i - 1]
        assert gap >= 0.04, f"Gap between calls {i-1} and {i} was only {gap:.4f}s"
