import asyncio

try:
    import uvloop
except ImportError:
    uvloop = None

import pytest

import asyncio_atexit

loop_factories = [pytest.param(asyncio.new_event_loop, id="default")]
if uvloop is not None:
    loop_factories.append(pytest.param(uvloop.new_event_loop, id="uvloop"))


@pytest.fixture(params=loop_factories)
def loop_factory(request):
    return request.param


def _run(coro, loop_factory):
    loop = loop_factory()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_asyncio_atexit(loop_factory):
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

    _run(test(), loop_factory)
    assert sync_called
    assert async_called


def test_unregister(loop_factory):
    sync_called = False

    def sync_cb():
        nonlocal sync_called
        sync_called = True

    async def test():
        asyncio_atexit.register(sync_cb)
        asyncio_atexit.register(sync_cb)
        asyncio_atexit.unregister(sync_cb)

    _run(test(), loop_factory)
    assert not sync_called


def test_run_raises(loop_factory):
    sync_called = False

    def sync_cb():
        nonlocal sync_called
        sync_called = True

    async def test():
        asyncio_atexit.register(sync_cb)
        1 / 0

    with pytest.raises(ZeroDivisionError):
        _run(test(), loop_factory)

    assert sync_called
