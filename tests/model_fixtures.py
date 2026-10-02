"""Spawn-importable synthetic backends. No model weights or network access."""
import os
import time


class SyntheticBackend:
    def invoke(self, operation, content):
        if content == "sleep":
            time.sleep(5)
        if content == "brief":
            time.sleep(0.3)
        if content == "fail":
            raise ValueError("synthetic-private-detail")
        if content == "crash":
            os._exit(7)
        if content == "oversize":
            return {"res_content": "x" * 20000}
        if operation == "retrieve":
            return {"response": ["Synthetic product"]}
        return {"res_content": '{"sku":"synthetic-a"}'}

    def close(self):
        pass


def startup_failure():
    raise ValueError("synthetic-private-startup-detail")


def startup_stall():
    time.sleep(5)
    return SyntheticBackend()
