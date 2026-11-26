"""Diagnostic-only pytest plugin: make TestClient re-raise server exceptions.

Loaded with ``-p diag_plugin`` (never part of the test suite) so a swallowed
500 can be investigated on the machine where it reproduces.
"""

import starlette.testclient as starlette_tc
import fastapi.testclient as fastapi_tc


def _patch(cls) -> None:
    original = cls.__init__

    def __init__(self, *args, **kwargs):
        kwargs["raise_server_exceptions"] = True
        original(self, *args, **kwargs)

    cls.__init__ = __init__


_patch(starlette_tc.TestClient)
if fastapi_tc.TestClient is not starlette_tc.TestClient:
    _patch(fastapi_tc.TestClient)


import queue as _queue

_original_put_nowait = _queue.Queue.put_nowait


def _put_nowait(self, item):
    import sys

    print(f"DIAG_HEARTBEAT_MSG: {item!r}", file=sys.stderr, flush=True)
    return _original_put_nowait(self, item)


_queue.Queue.put_nowait = _put_nowait
