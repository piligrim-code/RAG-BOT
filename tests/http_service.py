"""Owned ephemeral loopback ASGI server with bounded startup/shutdown checks."""
from contextlib import contextmanager
import socket
from threading import Thread
import time

import httpx
import uvicorn


@contextmanager
def running_service(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    address = sock.getsockname()
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="critical", lifespan="on"))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("Synthetic model HTTP server did not start")
            time.sleep(0.01)
        with httpx.Client(base_url=f"http://127.0.0.1:{address[1]}", trust_env=False, timeout=10) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Synthetic model HTTP server did not shut down")
