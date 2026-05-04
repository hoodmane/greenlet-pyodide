# -*- coding: utf-8 -*-
"""Upstream-greenlet test suite, adapted for the Pyodide port.

The upstream test base class (:class:`greenlet.tests.TestCase`) does
extensive lifecycle accounting: it counts pending C-level cleanups,
total main greenlets across threads, leaked Python objects, etc. None
of that is meaningful (or even importable) for our pure-Python port,
so this stub provides only the bits the test files actually use:

* a ``TestCase`` alias with empty leak-tracking hooks
* a few feature flags (``PY312``, ``PY313``, ``PY314``)
* the ``RUNNING_ON_*`` flags
* a no-op ``wait_for_pending_cleanups``

Tests that rely on threads, the ``_greenlet`` C-API capsule,
sub-process scripts, generator slot, weakref slot, tracing, gc-callback
hooks, etc. are auto-skipped at collection time by ``conftest.py``.
"""

import os
import sys
import sysconfig
import unittest

PY312 = sys.version_info[:2] >= (3, 12)
PY313 = sys.version_info[:2] >= (3, 13)
PY314 = sys.version_info[:2] >= (3, 14)

WIN = sys.platform.startswith("win")
RUNNING_ON_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS")
RUNNING_ON_TRAVIS = os.environ.get("TRAVIS") or RUNNING_ON_GITHUB_ACTIONS
RUNNING_ON_APPVEYOR = os.environ.get("APPVEYOR")
RUNNING_ON_CI = RUNNING_ON_TRAVIS or RUNNING_ON_APPVEYOR
RUNNING_ON_MANYLINUX = os.environ.get("GREENLET_MANYLINUX")
RUNNING_ON_FREETHREAD_BUILD = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


class TestCase(unittest.TestCase):
    """Drop-in replacement for upstream's leak-tracking ``TestCase``."""

    cleanup_attempt_sleep_duration = 0.001
    cleanup_max_sleep_seconds = 1
    expect_greenlet_leak = False
    greenlets_before_test = 0
    threads_before_test = 0
    main_greenlets_before_test = 0

    def wait_for_pending_cleanups(self, *_a, **_kw):
        return None

    def count_objects(self, *_a, **_kw):
        return 0

    def count_greenlets(self):
        return 0

    def get_process_uss(self):
        raise unittest.SkipTest("uss not supported on this port")

    def run_script(self, *_a, **_kw):
        raise unittest.SkipTest("subprocess script tests not supported")

    def assertScriptRaises(self, *_a, **_kw):
        raise unittest.SkipTest("subprocess script tests not supported")
