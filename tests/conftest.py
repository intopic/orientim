# -*- coding: utf-8 -*-
"""Shared fixtures, so the suites run under pytest as well as directly.

Both audit files were written as scripts with a __main__ block, which is fine
for a person and useless for CI. This lets `pytest tests/` collect them without
taking the script form away from anyone who prefers it.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest       # noqa: E402
import labserver    # noqa: E402


@pytest.fixture(scope="session")
def real_sdk_server():
    """The vendors' own SDKs against a local server that speaks their protocol."""
    import threading
    import test_real_sdk as mod
    threading.Thread(target=mod.srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    yield mod
    mod.srv.shutdown()
    mod.srv.server_close()


@pytest.fixture(scope="session", autouse=True)
def lab():
    srv = labserver.start()
    time.sleep(0.4)
    yield srv
    srv.shutdown()
    srv.server_close()
