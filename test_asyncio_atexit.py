import asyncio
import os
import subprocess
import sys
import textwrap
import threading
import time

try:
    import uvloop
except ImportError:
    uvloop = None

import pytest

import asyncio_atexit

if sys.version_info >= (3, 7):
    asyncio_run = asyncio.run
else:

    def asyncio_run(coro):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(coro)
        finally:
            loop.close()


policies = ["default"]
if uvloop is not None:
    policies.append("uvloop")


@pytest.fixture(autouse=True)
def reset_watchdog_state():
    """
    The watchdog is process-global and ends in os._exit, so no test may leave it armed or
    enabled for the next one. Tests that need it either use a grace far longer than the suite
    (asserting it armed, never that it fired) or run in a subprocess.
    """
    asyncio_atexit._watchdog_grace_seconds = None
    asyncio_atexit._watchdog_armed = False
    yield
    asyncio_atexit._watchdog_grace_seconds = None
    asyncio_atexit._watchdog_armed = False


@pytest.fixture(params=policies)
def policy(request):
    before_policy = asyncio.get_event_loop_policy()
    if request.param == "default":
        policy = asyncio.DefaultEventLoopPolicy()
    elif request.param == "uvloop":
        policy = uvloop.EventLoopPolicy()
    asyncio.set_event_loop_policy(policy)
    yield
    asyncio.set_event_loop_policy(before_policy)


def test_asyncio_atexit(policy):
    sync_called = False
    async_called = False

    def sync_cb():
        nonlocal sync_called
        sync_called = True
        raise ValueError("Failure shouldn't prevent other callbacks")

    async def async_cb():
        nonlocal async_called
        async_called = True

    async def test():
        asyncio_atexit.register(sync_cb)
        asyncio_atexit.register(async_cb)

    asyncio_run(test())
    assert sync_called
    assert async_called


def test_unregister(policy):
    sync_called = False

    def sync_cb():
        nonlocal sync_called
        sync_called = True

    async def test():
        asyncio_atexit.register(sync_cb)
        asyncio_atexit.register(sync_cb)
        asyncio_atexit.unregister(sync_cb)

    asyncio_run(test())
    assert not sync_called


def test_run_raises(policy):
    sync_called = False

    def sync_cb():
        nonlocal sync_called
        sync_called = True

    async def test():
        asyncio_atexit.register(sync_cb)
        1 / 0

    with pytest.raises(ZeroDivisionError):
        asyncio_run(test())

    assert sync_called


def _time_close(register_callbacks):
    """Drive a real loop through the patched close, returning how long closing took."""
    loop = asyncio.new_event_loop()

    async def _setup():
        register_callbacks()

    loop.run_until_complete(_setup())
    started = time.monotonic()
    loop.close()
    return time.monotonic() - started


# ---------------------------------------------------------------------------
# I1: loop.close() returns within sum(timeouts) + epsilon.
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_i1_blocking_sync_callback_is_bounded(policy):
    """
    The incident shape, and the reason this is a fork rather than a patch.

    `ldclient.close()` blocks in a bare `threading.Event.wait()` on flush workers stuck in
    `getaddrinfo()`. Upstream runs sync callbacks inline on the loop thread, so the loop is
    pinned and any `wait_for` timer scheduled on it can never fire. Running them on a daemon
    thread is what makes the bound real.

    The release below only stops the test leaking a live thread; the daemon flag is what makes
    abandoning it safe in production.
    """
    release = threading.Event()
    entered = threading.Event()

    def hangs_forever():
        entered.set()
        release.wait(timeout=120)

    try:
        elapsed = _time_close(lambda: asyncio_atexit.register(hangs_forever, timeout=1))
        assert entered.is_set(), "the blocking callback never ran"
        assert (
            elapsed < 20
        ), f"close took {elapsed:.1f}s; the hung callback was not abandoned"
    finally:
        release.set()


@pytest.mark.timeout(60)
def test_i1_hanging_coroutine_callback_is_bounded(policy):
    async def never_resolves():
        await asyncio.Event().wait()

    elapsed = _time_close(lambda: asyncio_atexit.register(never_resolves, timeout=1))

    assert (
        elapsed < 20
    ), f"close took {elapsed:.1f}s; the hung coroutine was not abandoned"


@pytest.mark.timeout(90)
def test_i1_total_close_time_is_bounded_by_the_sum_of_timeouts(policy):
    """
    The bound is per callback, so several wedged callbacks add up rather than sharing a budget.
    Pinning the sum keeps that explicit: it is what sets the watchdog grace, and it is why the
    grace can be exceeded by enough hung callbacks (see the module docstring).
    """
    release = threading.Event()
    timeouts = [1, 1, 1]

    def hangs_forever():
        release.wait(timeout=120)

    def _register():
        for t in timeouts:
            asyncio_atexit.register(hangs_forever, timeout=t)

    try:
        elapsed = _time_close(_register)
        assert elapsed >= sum(timeouts) - 0.5, (
            f"close took only {elapsed:.1f}s for {len(timeouts)} hung callbacks; they did not "
            "each get their own bound"
        )
        assert (
            elapsed < sum(timeouts) + 15
        ), f"close took {elapsed:.1f}s, well past the {sum(timeouts)}s of declared bounds"
    finally:
        release.set()


# ---------------------------------------------------------------------------
# I2: a callback that overruns or raises never prevents a later one from running.
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_i2_a_hung_callback_does_not_prevent_later_callbacks(policy):
    release = threading.Event()
    after = []

    def hangs_forever():
        release.wait(timeout=120)

    def _register():
        asyncio_atexit.register(hangs_forever, timeout=1)
        asyncio_atexit.register(lambda: after.append("still ran"), timeout=5)

    try:
        _time_close(_register)
        assert after == ["still ran"], "callbacks after the hung one were skipped"
    finally:
        release.set()


# ---------------------------------------------------------------------------
# I3: no callback exception reaches threading.excepthook.
# ---------------------------------------------------------------------------


def test_i3_off_thread_exception_is_reported_not_left_to_excepthook(policy, caplog):
    """
    Sync callbacks now run on a worker thread, where an exception cannot reach the awaiting
    coroutine by itself. It has to be carried back and logged; letting it escape would print a
    stray traceback while cleanup otherwise looked clean.
    """
    escaped = []
    original_hook = threading.excepthook
    threading.excepthook = lambda args: escaped.append(args)

    def boom():
        raise RuntimeError("boom")

    try:
        with caplog.at_level("WARNING"):
            _time_close(lambda: asyncio_atexit.register(boom, timeout=5))
    finally:
        threading.excepthook = original_hook

    assert escaped == [], f"an exception escaped to threading.excepthook: {escaped}"
    assert any(
        "boom" in r.getMessage() for r in caplog.records
    ), f"the failure was not reported; saw: {[r.getMessage() for r in caplog.records]}"


def test_sync_callable_returning_an_awaitable_is_awaited(policy):
    """Upstream supports this shape (a partial over a coroutine function), so the fork must."""
    seen = []

    async def cleanup():
        seen.append("awaited")

    # A plain lambda -- not a coroutine function -- that hands back a coroutine.
    _time_close(lambda: asyncio_atexit.register(lambda: cleanup(), timeout=5))

    assert seen == ["awaited"]


def test_register_outside_a_running_loop_raises(policy):
    """
    Callers that may run outside a loop rely on catching this, so it is part of the contract.
    """
    with pytest.raises(RuntimeError):
        asyncio_atexit.register(lambda: None)


# ---------------------------------------------------------------------------
# I4 / I5: the exit watchdog.
# ---------------------------------------------------------------------------


def test_i5_watchdog_stays_disarmed_unless_enabled(policy):
    """
    Default-off matters: this ends in os._exit, and a process that closes a loop but keeps
    running must not be killed by it.
    """
    _time_close(lambda: asyncio_atexit.register(lambda: None, timeout=5))

    assert asyncio_atexit._watchdog_armed is False


def test_i4_watchdog_is_armed_before_callbacks_run(policy):
    """
    The ordering property that made the watchdog worth building into the dispatcher rather than
    registering it as another callback: as a callback it would sit behind whatever registered
    first, and a hang there would mean it never armed at all.
    """
    armed_when_callback_ran = []

    # Far longer than the suite: this asserts arming, and must never actually fire.
    asyncio_atexit.enable_exit_watchdog(grace_seconds=3600)

    _time_close(
        lambda: asyncio_atexit.register(
            lambda: armed_when_callback_ran.append(asyncio_atexit._watchdog_armed),
            timeout=5,
        )
    )

    assert armed_when_callback_ran == [True]


def test_arming_the_watchdog_is_idempotent(policy):
    asyncio_atexit.enable_exit_watchdog(grace_seconds=3600)

    assert asyncio_atexit.arm_exit_watchdog() is True
    assert asyncio_atexit.arm_exit_watchdog() is False


# The whole justification for os._exit lives or dies here: a process wedged in shutdown must
# actually die. Asserting that the watchdog *armed* would not have caught a watchdog that
# armed and then failed to kill anything, so this runs a real process to exhaustion.
_WEDGED_PROCESS = """
import asyncio, sys, threading
sys.path.insert(0, {repo!r})
import asyncio_atexit

asyncio_atexit.enable_exit_watchdog(grace_seconds=2)

def never_returns():
    threading.Event().wait()   # nothing will ever set this

async def main():
    asyncio_atexit.register(never_returns, timeout=3600)

loop = asyncio.new_event_loop()
loop.run_until_complete(main())
print("closing", flush=True)
loop.close()
print("SHOULD NEVER GET HERE", flush=True)
"""


@pytest.mark.timeout(120)
def test_watchdog_actually_terminates_a_wedged_process():
    """
    INVARIANT: a process whose shutdown is wedged exits anyway, within the grace.

    Note the callback's own timeout is 3600s, so the per-callback bound cannot be what saves
    this - only the watchdog can. That is the point: the watchdog covers hangs the per-callback
    bounds do not, which is why it exists at all.
    """
    repo = os.path.dirname(os.path.abspath(__file__))
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", _WEDGED_PROCESS.format(repo=repo)],
        capture_output=True,
        text=True,
        timeout=90,
    )
    elapsed = time.monotonic() - started

    assert (
        "closing" in result.stdout
    ), f"the child never reached loop.close(); stderr: {result.stderr}"
    assert (
        "SHOULD NEVER GET HERE" not in result.stdout
    ), "loop.close() returned, so nothing was wedged"
    assert result.returncode == 0, f"watchdog exited {result.returncode}, expected 0"
    assert elapsed < 60, f"process took {elapsed:.1f}s to die against a 2s grace"
