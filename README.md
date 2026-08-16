# asyncio atexit

Adds atexit functionality to asyncio:

```python
import asyncio_atexit

async def close_db():
    await db_connection.close()

asyncio_atexit.register(close_db)
```

[atexit][] is part of the standard library,
and gives you a way to register functions to call when the interpreter exits.

[atexit]: https://docs.python.org/3/library/atexit.html

asyncio doesn't have equivalent functionality to register functions
when the _event loop_ exits:

This package adds functionality that can be considered equivalent to `atexit.register`,
but tied to the event loop lifecycle. It:

1. accepts both coroutines and synchronous functions
1. should be called from a running event loop
1. calls registered cleanup functions when the event loop closes
1. only works if the application running the event loop calls `close()`

## Bounded cleanup

Cleanup callbacks are bounded, so one that blocks can't stop the loop from closing:

```python
# Per-callback bound; the default is 10s.
asyncio_atexit.register(close_db, timeout=5)
```

This matters because a callback that never returns means the loop never closes and the process
never exits — and where a supervisor reclaims resources on process exit (a job runner's
concurrency slot, a container slot, a lock), one wedged callback leaks that resource forever.

A real example: a LaunchDarkly client closed from a cleanup callback. Under degraded DNS,
`ldclient.close()` → `EventProcessor.stop()` → `FixedThreadPool.wait()` is a bare
`threading.Event.wait()` with no timeout, waiting on flush workers stuck in `getaddrinfo()`.
urllib3's connect/read timeouts don't bound name resolution, so it never returned.

Note that a timeout *alone* wouldn't have fixed this. Callbacks used to be invoked on the loop
thread, so a synchronous blocking one pinned that thread — and `asyncio.wait_for`'s timeout is
a timer scheduled on the same loop, which can then never run it. Measured with an 8s blocking
callback and a declared 1s bound:

| | `loop.close()` took |
|---|---|
| before | **8.00s** |
| before, with the caller wrapping in `asyncio.wait_for` | **8.01s** |
| after | **1.00s** |

Sync callbacks now run on a daemon thread, which is what makes the bound real.

## Exit watchdog

For processes whose loop close means "we're exiting", an optional watchdog forces exit if
shutdown wedges anyway — covering the real `loop.close()`, interpreter finalization, and hangs
that aren't in a cleanup callback at all:

```python
asyncio_atexit.enable_exit_watchdog(grace_seconds=90)
```

Off by default: it ends in `os._exit`, so a process that closes a loop and then keeps running
must not be killed by it. It's armed *before the first callback runs*, so a callback hanging
can't prevent it from arming.

## Invariants

Each has a test named after it in `test_asyncio_atexit.py`:

| | |
|---|---|
| **I1** | `loop.close()` returns within `sum(timeouts) + epsilon`, whatever the callbacks do |
| **I2** | A callback that overruns or raises never prevents a later callback from running |
| **I3** | No callback exception reaches `threading.excepthook` |
| **I4** | The watchdog is armed before the first callback runs |
| **I5** | The watchdog never arms unless the process explicitly opts in |

### Two bounds, and which one wins

The per-callback timeout and the watchdog grace are independent and can disagree: N callbacks
each allowed the default 10s can sum past a 90s grace.

When they disagree **the watchdog wins, by design**. The per-callback timeout bounds one hook
so the *others still get to run* (I2); the watchdog bounds the process so a supervisor can
reclaim it, and it's a backstop rather than a participant. A process already wedged for the
full grace has nothing left worth waiting for.
