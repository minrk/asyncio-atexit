import asyncio
import sys

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
