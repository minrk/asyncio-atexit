"""
asyncio_atexit: atexit for asyncio
"""

import asyncio
import inspect
import sys
import weakref
from functools import partial

__all__ = ["register", "unregister"]
__version__ = "1.0.1"

_registry = weakref.WeakKeyDictionary()

if sys.version_info < (3, 7):
    get_running_loop = asyncio.get_event_loop
else:
    get_running_loop = asyncio.get_running_loop


class _RegistryEntry:
    def __init__(self, loop):
        try:
            self._close_ref = weakref.WeakMethod(loop.close)
        except TypeError:
            # not everything can be weakref'd (Extensions such as uvloop).
            # Hold a regular reference _on the object_, in those cases
            loop._atexit_orig_close = loop.close
            self._close_ref = lambda: loop._atexit_orig_close
        self.callbacks = []

    def close(self):
        return self._close_ref()()


def register(callback, *, loop=None):
    """
    Register a callback for when the current event loop closes

    Like atexit.register, but run when the asyncio loop is closing,
    rather than process cleanup.

    `loop` may be specified as a keyword arg
    to attach to a non-running event loop.

    Allows coroutines to cleanup their resources.

    Callback will be passed no arguments.
    To pass arguments to your callback,
    use `functools.partial`.
    """
    entry = _get_entry(loop)
    entry.callbacks.append(callback)


def unregister(callback, *, loop=None):
    """
    Unregister a callback registered with asyncio_atexit.register

    `loop` may be specified as a keyword arg
    to attach to a non-running event loop.
    """

    entry = _get_entry(loop)
    # remove all instances of the callback
    while True:
        try:
            entry.callbacks.remove(callback)
        except ValueError:
            break


def _get_entry(loop=None):
    """Get the registry entry for an event loop"""
    if loop is None:
        loop = get_running_loop()
    _register_loop(loop)
    return _registry[loop]


def _register_loop(loop):
    """Patch an asyncio.EventLoop to support atexit callbacks"""
    if loop in _registry:
        return

    _registry[loop] = _RegistryEntry(loop)

    loop.close = partial(_asyncio_atexit_close, loop)


async def _run_asyncio_atexits(loop, callbacks):
    """Run asyncio atexit callbacks

    This runs in EventLoop.close() prior to actually closing the loop
    """
    for callback in callbacks:
        try:
            f = callback()
            if inspect.isawaitable(f):
                await f
        except Exception as e:
            print(
                f"Unhandled exception in asyncio atexit callback {callback}: {e}",
                file=sys.stderr,
            )


def _asyncio_atexit_close(loop):
    """Patched EventLoop.close method to run atexit callbacks

    prior to the unpatched close method.
    """
    entry = _get_entry(loop)
    if entry.callbacks:
        loop.run_until_complete(_run_asyncio_atexits(loop, entry.callbacks))
    entry.callbacks[:] = []
    return entry.close()
