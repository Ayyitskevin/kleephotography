"""Unit tests for the external dead-man's switch.

No network: `urllib.request.urlopen` is monkeypatched at the module seam.

The contract worth protecting here is mostly about what the ping must NOT do.
It is the alarm that fires when the host is dead, so it must stay armed when
Telegram is not configured, and it must never be able to break, block, or fail
the loop whose liveness it reports.
"""

import threading
import time

import pytest

from app import config, deadman, features

pytestmark = pytest.mark.unit


class _Resp:
    def read(self) -> bytes:
        return b"ok"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _answers(monkeypatch, captured=None, exc=None):
    """Point the urllib seam at a canned response.

    `captured["done"]` is always set before returning or raising, so a test can
    join the worker thread instead of sleeping. That matters more here than
    usual: this seam is the process-wide urllib.request, so a thread still in
    flight at monkeypatch teardown would fall through to the real network.
    """
    captured = captured if captured is not None else {}
    captured.setdefault("done", threading.Event())

    def fake_urlopen(url, timeout):
        captured["url"] = url
        captured["timeout"] = timeout
        captured["done"].set()
        if exc is not None:
            raise exc
        return _Resp()

    monkeypatch.setattr(deadman.urllib.request, "urlopen", fake_urlopen)
    return captured


def test_dormant_when_url_unset(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_PING_URL", "")
    fired = threading.Event()
    monkeypatch.setattr(deadman.urllib.request, "urlopen", lambda *a, **k: fired.set())
    deadman.ping()
    assert not fired.wait(0.2), "dormant switch still called out"


def test_pings_configured_url(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_PING_URL", "https://hc.test/abc")
    captured = _answers(monkeypatch)
    deadman.ping()
    assert captured["done"].wait(2), "ping thread never ran"
    assert captured["url"] == "https://hc.test/abc"
    assert captured["timeout"] == deadman._TIMEOUT


def test_stays_armed_without_telegram(monkeypatch):
    """The whole point: this alarm must not depend on the host's own alerting.

    If it were gated on Telegram, the dead-man's switch would go silent in
    exactly the misconfiguration it exists to survive.
    """
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(config, "HEARTBEAT_PING_URL", "https://hc.test/abc")
    assert features.telegram_enabled() is False
    assert features.deadman_enabled() is True


def test_network_failure_never_raises(monkeypatch):
    """A down monitoring service must not take out the loop it is monitoring."""
    monkeypatch.setattr(config, "HEARTBEAT_PING_URL", "https://hc.test/abc")
    captured = _answers(monkeypatch, exc=OSError("connection refused"))
    deadman.ping()  # must not raise, here or on the worker thread
    assert captured["done"].wait(2), "ping thread never ran"


def test_non_http_scheme_refused(monkeypatch):
    """A typo'd URL must not turn the ping into a local file read."""
    fired = threading.Event()
    monkeypatch.setattr(deadman.urllib.request, "urlopen", lambda *a, **k: fired.set())
    deadman.ping("file:///etc/passwd")
    assert not fired.wait(0.2), "non-http URL was fetched"


def test_slow_service_does_not_block_caller(monkeypatch):
    """Fire-and-forget: the scheduler tick must not wait on the monitor."""
    monkeypatch.setattr(config, "HEARTBEAT_PING_URL", "https://hc.test/abc")
    release = threading.Event()
    entered = threading.Event()
    finished = threading.Event()

    def slow_urlopen(url, timeout):
        entered.set()
        release.wait(5)
        finished.set()
        return _Resp()

    monkeypatch.setattr(deadman.urllib.request, "urlopen", slow_urlopen)
    t0 = time.monotonic()
    deadman.ping()
    elapsed = time.monotonic() - t0
    assert entered.wait(2), "ping thread never ran"
    release.set()
    assert finished.wait(2), "worker still parked in urlopen at teardown"
    assert elapsed < 0.5, f"ping blocked the caller for {elapsed:.2f}s"
