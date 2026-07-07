"""Shared pytest configuration for the greenlet-pyodide test suite.

When the tests run inside Pyodide, ``import greenlet`` resolves to the
in-tree pure-Python implementation under ``src/greenlet`` because
``run_tests.mjs`` puts ``src`` on ``sys.path``.

When the tests run on native CPython, ``import greenlet`` resolves to
the upstream C extension installed via the dev dependency group. Tests
marked ``@pytest.mark.pyodide`` are skipped automatically because they
rely on ``pyodide.ffi.run_sync`` and ``asyncio`` interop that only
makes sense inside Pyodide.
"""

import sys

import pytest


def _running_in_pyodide() -> bool:
    return "pyodide" in sys.modules or sys.platform == "emscripten"


def pytest_collection_modifyitems(config, items):
    if _running_in_pyodide():
        return
    skip_pyodide = pytest.mark.skip(reason="requires Pyodide runtime (pyodide.ffi.run_sync)")
    for item in items:
        if "pyodide" in item.keywords:
            item.add_marker(skip_pyodide)
