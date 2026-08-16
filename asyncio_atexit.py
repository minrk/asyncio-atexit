"""
asyncio_atexit: atexit for asyncio, with time bounds and an exit watchdog.

WHY CLEANUP NEEDS A BOUND
------------------------
A cleanup callback that blocks means the event loop never closes and the process never exits.
Where a supervisor reclaims resources on process exit -- a job runner's concurrency slot, a
container slot, a lock -- one wedged callback leaks that resource permanently.

A real example. A LaunchDarkly client closed from an atexit callback: under degraded DNS,
`ldclient.close()` -> `EventProcessor.stop()` -> `FixedThreadPool.wait()` is a bare
`threading.Event.wait()` with no timeout, waiting on flush-worker threads stuck in
`getaddrinfo()` (urllib3's connect/read timeouts do not bound name resolution). The process
hung forever. Its supervisor released a concurrency slot only on process exit, so every
occurrence permanently consumed one slot until the pool was fully wedged.

WHY A TIMEOUT ALONE IS NOT ENOUGH
---------------------------------
The obvious fix is to wrap the await in `asyncio.wait_for`. That does not work, for two
compounding reasons:

1. Callbacks were invoked as `f = callback()` on the loop thread, awaiting only if the result
   is awaitable. A *synchronous* blocking callback therefore pins the loop thread outright.
2. `wait_for`'s timeout is itself a timer callback scheduled on that same loop. A blocked loop
   thread can never run it.

So a timeout bounds only callbacks that yield to the loop -- which is precisely the class that
does not cause this failure. Measured, with an 8s blocking callback and a declared 1s bound:

    before                       8.00s
    before + caller's wait_for   8.01s
    after                        1.00s

Running sync callbacks off the loop thread is what makes the bound real, and that is why this
changes the dispatcher rather than just wrapping the await.

WHAT THIS ADDS
--------------
- Sync callbacks run on a daemon thread, so a blocking one cannot pin the loop and the timeout
  can actually fire. Deliberately not the default executor: `concurrent.futures` joins its
  worker threads at process exit, which would reintroduce the very hang being prevented.
- A per-callback deadline, after which the callback is abandoned and the remaining ones still
  run.
- Off-thread exceptions are captured and re-raised on the loop rather than escaping to
  `threading.excepthook`, which would print an unhandled traceback while cleanup logged
  success.
- An opt-in exit watchdog, armed before the first callback runs, so it also covers the real
  `loop.close()`, interpreter finalization, and hangs of shapes nobody has characterized.

All of this is backward compatible: `register`/`unregister` keep their existing signatures,
with `timeout` added as a keyword argument, and the watchdog is off unless opted in.

INVARIANTS
----------
Each has a test named after it in `test_asyncio_atexit.py`.

I1. `loop.close()` returns within `sum(timeouts) + epsilon`, whatever the callbacks do.
I2. A callback that overruns or raises never prevents a later callback from running. Cleanup
    is a list of independent obligations, not a chain.
I3. No callback exception reaches `threading.excepthook`.
I4. The watchdog is armed before the first callback runs.
I5. The watchdog never arms unless the process explicitly opts in.

TWO BOUNDS, AND WHICH ONE WINS
------------------------------
The per-callback timeout and the watchdog grace are independent and can disagree: N callbacks
each allowed `DEFAULT_CALLBACK_TIMEOUT_SECONDS` can sum past `DEFAULT_EXIT_GRACE_SECONDS`.

When they disagree the watchdog wins, by design. The per-callback timeout bounds one hook so
the *others still get to run* (I2); the watchdog bounds the process so a supervisor can reclaim
it, and it is a backstop rather than a participant. A process already wedged for the full grace
has nothing left worth waiting for, so cutting cleanup short there is the correct trade rather
than a misconfiguration.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
import threading
import time
import weakref
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = ["register", "unregister", "enable_exit_watchdog", "arm_exit_watchdog"]
__version__ = "1.0.1"

log = logging.getLogger(__name__)

# Long enough that healthy network cleanup finishes well inside it, short enough that a wedged
# one does not outlive the reason anyone is waiting.
DEFAULT_CALLBACK_TIMEOUT_SECONDS: float = 10.0

# Must clear normal teardown for the host application or healthy processes get killed. The leak
# it prevents is unbounded, so erring high costs little.
DEFAULT_EXIT_GRACE_SECONDS: float = 90.0

_registry: weakref.WeakKeyDictionary[Any, _RegistryEntry] = weakref.WeakKeyDictionary()

_watchdog_lock = threading.Lock()
_watchdog_grace_seconds: float | None = None
_watchdog_armed = False


class _RegistryEntry:
    """
    A loop's callbacks plus a reference to its original, unpatched `close`.

    The weakref dance is upstream's, kept as-is: uvloop's `close` cannot be weakref'd, so for
    those loops the original is stashed on the object instead.
    """

    def __init__(self, loop: Any) -> None:
        try:
            self._close_ref = weakref.WeakMethod(loop.close)
        except TypeError:
            # Not everything can be weakref'd (extensions such as uvloop). Hold a regular
            # reference _on the object_ in those cases.
            loop._atexit_orig_close = loop.close
            self._close_ref = lambda: loop._atexit_orig_close
        self.callbacks: list[tuple[Callable[[], Any], float]] = []

    def close(self) -> Any:
        original_close = self._close_ref()
        if original_close is None:
            # The weakref'd loop was collected out from under us, so there is nothing left to
            # close. Upstream calls straight through and would raise TypeError here.
            return None
        return original_close()


def register(
    callback: Callable[[], Any],
    *,
    loop: asyncio.AbstractEventLoop | None = None,
    timeout: float = DEFAULT_CALLBACK_TIMEOUT_SECONDS,
) -> None:
    """
    Register a callback to run when the current event loop closes.

    Drop-in for upstream's `register`, with `timeout` added: the callback is abandoned after
    that many seconds and the remaining callbacks still run (I1, I2). Coroutine functions and
    plain functions are both accepted; plain ones run on a daemon thread so a blocking call
    cannot pin the loop.

    Raises RuntimeError if there is no running loop and none was passed -- callers that may run
    outside a loop should catch it, as upstream's contract already required.
    """
    entry = _get_entry(loop)
    entry.callbacks.append((callback, timeout))


def unregister(
    callback: Callable[[], Any],
    *,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """
    Unregister every registration of `callback`, whatever timeout it was registered with.
    """
    entry = _get_entry(loop)
    entry.callbacks[:] = [(cb, t) for (cb, t) in entry.callbacks if cb != callback]


def enable_exit_watchdog(grace_seconds: float = DEFAULT_EXIT_GRACE_SECONDS) -> None:
    """
    Opt this process in to the exit watchdog, armed when a loop begins closing (I5).

    Off by default and deliberately not automatic: this ends in `os._exit`, so a process that
    closes a loop and then keeps running -- a sync entrypoint using `asyncio.run`, say -- would
    otherwise be killed mid-life. Only a process whose loop close genuinely means "we are
    exiting" should call this.
    """
    global _watchdog_grace_seconds
    with _watchdog_lock:
        _watchdog_grace_seconds = grace_seconds


def arm_exit_watchdog(grace_seconds: float | None = None) -> bool:
    """
    Start the clock on process exit. Returns immediately; True if this call armed it.

    Idempotent, so arming from both the dispatcher and an explicit call is safe.
    """
    global _watchdog_armed

    with _watchdog_lock:
        grace = grace_seconds if grace_seconds is not None else _watchdog_grace_seconds
        if grace is None or _watchdog_armed:
            return False
        _watchdog_armed = True

    def _force_exit() -> None:
        # A daemon thread cannot itself hold the process open, so a healthy exit simply
        # discards this thread mid-sleep. That is why there is no disarm path to get wrong.
        time.sleep(grace)
        log.error(
            "Process still alive %ss after its event loop began closing; forcing exit. "
            "Something in shutdown is wedged - without this the process would hang forever, "
            "and any supervisor that reclaims resources on process exit would never get "
            "them back.",
            grace,
        )
        sys.stderr.flush()
        # os._exit, not sys.exit: sys.exit merely raises SystemExit on *this* thread and would
        # leave the stuck main thread untouched. The raw syscall also skips atexit hooks and
        # loop close, which is the point - that machinery is what is hung.
        os._exit(_WATCHDOG_EXIT_CODE)

    threading.Thread(
        target=_force_exit, daemon=True, name="asyncio-atexit-watchdog"
    ).start()
    return True


# Exit 0 by default: a wedged *shutdown* usually follows work that already succeeded and was
# already reported, and a non-zero code can make a supervisor overwrite that outcome with a
# crash. Override where a forced exit should be treated as failure.
_WATCHDOG_EXIT_CODE = 0


def set_watchdog_exit_code(code: int) -> None:
    """
    Set the exit code the watchdog uses. See `_WATCHDOG_EXIT_CODE` for why 0 is the default.
    """
    global _WATCHDOG_EXIT_CODE
    _WATCHDOG_EXIT_CODE = code


def _get_entry(loop: asyncio.AbstractEventLoop | None = None) -> _RegistryEntry:
    """Get the registry entry for an event loop."""
    if loop is None:
        loop = asyncio.get_running_loop()
    _register_loop(loop)
    return _registry[loop]


def _register_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Patch an event loop to support atexit callbacks."""
    if loop in _registry:
        return
    _registry[loop] = _RegistryEntry(loop)
    # Note this makes the loop strongly reference itself (loop -> close -> partial -> loop), so
    # the WeakKeyDictionary entry is only reclaimed once the cycle collector runs. That is
    # upstream's behaviour and is left alone deliberately: the weakref above is about not
    # resurrecting a dead loop's bound method, not about promptness.
    loop.close = partial(_asyncio_atexit_close, loop)  # type: ignore[method-assign]


def _describe(callback: Callable[[], Any]) -> str:
    return getattr(callback, "__qualname__", None) or repr(callback)


async def _run_in_daemon_thread(fn: Callable[[], Any], *, timeout: float) -> Any:
    """
    Run a synchronous callable off the loop thread, bounded by `timeout`.

    A daemon thread rather than the default executor: `concurrent.futures` joins executor
    workers at interpreter exit, so a stuck job there would hang the process anyway - the exact
    failure this module exists to prevent.
    """
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["result"] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised on the loop below (I3)
            # An exception here cannot reach the awaiting coroutine on its own. Letting it
            # escape would print an unhandled-thread traceback while cleanup reported success.
            box["error"] = e
        finally:
            try:
                loop.call_soon_threadsafe(done.set)
            except RuntimeError:
                # We were abandoned at the deadline and the loop has since closed, so there is
                # nobody left to notify. Signalling anyway raises here, on a thread with no
                # handler - the stray traceback this module is otherwise careful to avoid.
                pass

    threading.Thread(
        target=_run, daemon=True, name=f"atexit-{_describe(fn)[:40]}"
    ).start()

    # Raises asyncio.TimeoutError, which the dispatcher turns into "abandoned, carry on".
    await asyncio.wait_for(done.wait(), timeout)

    if "error" in box:
        raise box["error"]
    return box.get("result")


async def _call_bounded(callback: Callable[[], Any], timeout: float) -> None:
    """
    Invoke one callback under a single deadline covering both of its phases.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    if asyncio.iscoroutinefunction(callback):
        # Calling a coroutine function only builds the coroutine; nothing runs yet, so this
        # cannot block.
        result: Any = callback()
    else:
        result = await _run_in_daemon_thread(callback, timeout=timeout)

    # A sync callable may still hand back an awaitable (functools.partial over a coroutine
    # function, say). Await it on the loop, under whatever of the deadline is left, so the two
    # phases share one budget rather than getting one each.
    if inspect.isawaitable(result):
        await asyncio.wait_for(result, max(0.0, deadline - loop.time()))


async def _run_asyncio_atexits(
    loop: asyncio.AbstractEventLoop, callbacks: list[tuple[Callable[[], Any], float]]
) -> None:
    """
    Run atexit callbacks, bounded and independent (I1, I2).

    This runs in EventLoop.close() prior to actually closing the loop.
    """
    for callback, timeout in callbacks:
        try:
            await _call_bounded(callback, timeout)
        except asyncio.TimeoutError:
            log.warning(
                "asyncio atexit callback %s exceeded %ss; abandoning it and continuing with "
                "the remaining callbacks.",
                _describe(callback),
                timeout,
            )
        except Exception as e:
            log.warning(
                "Unhandled exception in asyncio atexit callback %s: %s",
                _describe(callback),
                e,
            )
        # BaseException (SystemExit, KeyboardInterrupt) is deliberately NOT caught: those mean
        # "stop everything", not "this obligation failed", so they abort the remaining
        # callbacks rather than being logged and stepped over.


def _asyncio_atexit_close(loop: asyncio.AbstractEventLoop) -> Any:
    """
    Patched EventLoop.close: arm the watchdog, run callbacks, then close for real.
    """
    entry = _get_entry(loop)

    # Before the first callback (I4), so the watchdog covers the callbacks themselves, the real
    # close below, and interpreter finalization. Being inside the dispatcher is what makes this
    # independent of registration order - nothing can register ahead of it.
    arm_exit_watchdog()

    if entry.callbacks:
        callbacks = list(entry.callbacks)
        # Cleared before running so a re-entrant close cannot run them twice.
        entry.callbacks[:] = []
        loop.run_until_complete(_run_asyncio_atexits(loop, callbacks))

    return entry.close()
