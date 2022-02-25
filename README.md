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
