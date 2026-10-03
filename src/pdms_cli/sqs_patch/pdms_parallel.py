"""Loaded by this folder's sitecustomize.py in services run by pdms: two speed-ups for slow databases, each turned on
by its own variable, without touching the service code.

``PDMS_PARALLEL_REQUESTS=1``: every HTTP request runs in a worker thread with its own event loop. PDMS services call
SQLAlchemy (and requests, boto3...) synchronously inside ``async`` endpoints, so one slow query used to hold up every
other request of the service; now it only holds up its own. ``receive`` and ``send`` still run on uvicorn's loop.

``PDMS_WARM_CONNECTIONS=<n>``: each SQLAlchemy engine the service keeps opens ``n`` connections right after it
starts, so the first requests don't pay for opening them (a dozen round trips each, many seconds through a tunnel).
"""

import asyncio
import contextvars
import gc
import os
import sys
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor

REQUEST_THREADS = 16  # about the default SQLAlchemy pool of PDMS services (5 + 10 overflow)
WARM_DELAY = 1.0  # seconds: the app has booted by then (snakesdk builds a second set of sources and drops it)


def log(message):
    print(f"[pdms] {message}", file=sys.stderr, flush=True)


def run_requests_in_threads(threads=REQUEST_THREADS):
    import starlette.applications

    original = starlette.applications.Starlette.__call__
    if getattr(original, "_pdms_parallel", False):
        return
    pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="pdms-request")
    local = threading.local()

    def thread_loop():
        loop = getattr(local, "loop", None)
        if loop is None:
            loop = local.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":  # lifespan and websockets stay on uvicorn's loop
            return await original(self, scope, receive, send)
        main = asyncio.get_running_loop()

        def on_main(function):
            async def call(*args):
                return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(function(*args), main))

            return call

        def run():
            return thread_loop().run_until_complete(original(self, scope, on_main(receive), on_main(send)))

        await main.run_in_executor(pool, contextvars.copy_context().run, run)

    setattr(__call__, "_pdms_parallel", True)
    starlette.applications.Starlette.__call__ = __call__
    log(f"requests run in parallel ({threads} threads)")


def warm(engine_ref, count, delay=WARM_DELAY):
    """Open ``count`` connections of the engine at once and give them back to its pool."""
    time.sleep(delay)
    gc.collect()  # dropped engines live in reference cycles: free them so only the ones in use get connections
    engine = engine_ref()
    if engine is None:
        return
    size = getattr(engine.pool, "size", lambda: count)()
    count = min(count, size)
    opened, failed = [], []

    def connect():
        try:
            opened.append(engine.connect())
        except Exception as exc:  # noqa: BLE001 - the service will report it on its first query anyway
            failed.append(exc)

    start = time.monotonic()
    threads = [threading.Thread(target=connect, daemon=True) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for connection in opened:
        connection.close()
    seconds = time.monotonic() - start
    if opened:
        log(f"opened {len(opened)} database connection(s) in {seconds:.1f} s")
    if failed:
        log(f"could not open {len(failed)} database connection(s): {failed[0]}")


def warm_new_engines(count):
    import sqlalchemy
    import sqlalchemy.engine

    original = sqlalchemy.create_engine
    if getattr(original, "_pdms_warm", False):
        return

    def create_engine(*args, **kwargs):
        engine = original(*args, **kwargs)
        threading.Thread(target=warm, args=(weakref.ref(engine), count), daemon=True,
                         name="pdms-warm-connections").start()
        return engine

    setattr(create_engine, "_pdms_warm", True)
    for module in (sqlalchemy, sqlalchemy.engine):
        if getattr(module, "create_engine", None) is original:
            setattr(module, "create_engine", create_engine)


def install():
    if os.environ.get("PDMS_PARALLEL_REQUESTS") == "1":
        try:
            run_requests_in_threads()
        except ImportError:  # not a web service (poetry itself, a consumer...)
            pass
    count = int(os.environ.get("PDMS_WARM_CONNECTIONS") or 0)
    if count > 0:
        try:
            warm_new_engines(count)
        except ImportError:  # no SQLAlchemy here
            pass
