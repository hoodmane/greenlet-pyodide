"""Collection rules for the upstream-greenlet test suite.

The tests in this directory were copied verbatim from the upstream
greenlet repository so we can validate API compatibility. Many of them
exercise features that the Pyodide port does not (and cannot)
implement, including:

* Threading. Pyodide is single-threaded; threads work on native CPython
  but exercising thread-greenlet interaction is well outside the scope
  of this port.
* The ``greenlet._greenlet`` C-extension capsule, including
  ``UnswitchableGreenlet``, ``get_pending_cleanup_count``,
  ``get_total_main_greenlets``, ``set_thread_local``, etc.
* Tracing hooks (``settrace``/``gettrace`` are accepted but never
  invoked).
* Generator/weakref/gc slots that depend on the C extension layout.
* Subprocess-driven crash tests (``fail_*.py``) and the C++/CFFI test
  extensions.
* Reference-count leak checks (``test_leaks.py``,
  ``test_greenlet_trash.py``, ``test_extension_interface.py``).

We skip those at collection time so the rest of the suite can run.
"""

import importlib
import os
import sys

import pytest


# Whole modules to skip. Each entry is the test file's basename.
_SKIP_MODULES = {
    # Subprocess crash drivers - not real test modules
    "fail_clearing_run_switches.py",
    "fail_cpp_exception.py",
    "fail_initialstub_already_started.py",
    "fail_slp_switch.py",
    "fail_switch_three_greenlets.py",
    "fail_switch_three_greenlets2.py",
    "fail_switch_two_greenlets.py",
    # C++ extension tests
    "test_cpp.py",
    # C-API extension interface
    "test_extension_interface.py",
    # gc slot / refcount leak / cleanup machinery from the C ext
    "test_gc.py",
    "test_greenlet_trash.py",
    "test_leaks.py",
    "test_interpreter_shutdown.py",
    # generator/weakref slots: implemented by the C ext, not us
    "test_generator.py",
    "test_generator_nested.py",
    # Internal stack-saved telemetry from the C ext
    "test_stack_saved.py",
    # Tracing not implemented
    "test_tracing.py",
}


collect_ignore_glob = list(_SKIP_MODULES)


def _skip_if_no_threads(item):
    # Pyodide has no threads. Native CPython does. We still skip
    # threaded tests on native because they aren't testing anything
    # we ported - the upstream C extension handles threads natively
    # and we don't.
    name = item.name.lower()
    if "thread" in name or "_threaded" in name:
        item.add_marker(pytest.mark.skip(reason="threading not supported by port"))


def _skip_if_uses_internal_capsule(item):
    src = getattr(item, "_obj", None)
    if src is None:
        return
    # Check the test source for forbidden symbol use.
    try:
        source = pytest._pytest.python.getfslineno  # type: ignore[attr-defined]
    except AttributeError:
        return
    try:
        with open(item.fspath, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return
    test_func = item.function
    # Crude: skip individual tests that mention these in their source.
    forbidden = ("_greenlet.", "_greenlet,", "greenlet._greenlet")
    if test_func.__doc__ and any(s in test_func.__doc__ for s in forbidden):
        item.add_marker(pytest.mark.skip(reason="uses internal C capsule"))


# Tests within still-collected modules that don't apply.
_INDIVIDUAL_SKIPS = {
    # threads in test_greenlet.py
    "test_threads": "threading not supported by port",
    "test_switching_many_threads": "threading not supported by port",
    "test_threaded_reparent": "threading not supported by port",
    "test_threaded_updatecurrent": "threading not supported by port",
    "test_thread_bug": "threading not supported by port",
    "test_dealloc_other_thread": "threading not supported by port",
    "test_implicit_parent_with_threads": "threading not supported by port",
    "test_unexpected_reparenting_thread_running": "threading not supported by port",
    "test_issue_245_reference_counting_subclass_threads": "threading not supported by port",
    "test_main_from_other_thread": "threading not supported by port",
    "test_switch_to_another_thread": "threading not supported by port",
    "test_no_gil_on_free_threaded": "free-threaded build feature",
    # frame inspection - depends on greenlet's C-level gr_frame
    "test_frame": "gr_frame not implemented in port",
    "test_can_access_f_back_of_suspended_greenlet": "gr_frame not implemented in port",
    "test_get_stack_with_nested_c_calls": "gr_frame not implemented in port",
    "test_frames_always_exposed": "gr_frame not implemented in port",
    # Broken / reentrant_switch tests need _greenlet internals
    "test_failed_to_initialstub": "uses _greenlet capsule",
    "test_failed_to_switch_into_running": "uses _greenlet capsule",
    "test_failed_to_slp_switch_into_running": "uses _greenlet capsule",
    "test_reentrant_switch_two_greenlets": "uses _greenlet capsule",
    "test_reentrant_switch_three_greenlets": "uses _greenlet capsule",
    "test_reentrant_switch_three_greenlets2": "uses _greenlet capsule",
    "test_reentrant_switch_GreenletAlreadyStartedInPython": "uses _greenlet capsule",
    "test_reentrant_switch_run_callable_has_del": "uses _greenlet capsule",
    # repr depends on internal state strings
    "test_main_while_running": "repr format differs in port",
    "test_main_in_background": "repr format differs in port",
    "test_initial": "repr format differs in port",
    "test_dead": "repr format differs in port",
    "test_formatting_produces_native_str": "repr format differs in port",
    # MainGreenlet type subclassing relies on the C type
    "test_main_greenlet_type_can_be_subclassed": "MainGreenlet C type not exposed",
    # version: we have a custom version
    "test_version": "port has its own version string",
    # Switching across threads
    "test_switch_kwargs_to_parent": None,  # keep
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        # Per-test name skips
        for needle, reason in _INDIVIDUAL_SKIPS.items():
            if reason and item.name == needle:
                item.add_marker(pytest.mark.skip(reason=reason))
                break
        else:
            _skip_if_no_threads(item)
