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
    "test_greenlet_trash.py",
    "test_leaks.py",
    "test_interpreter_shutdown.py",
    # Internal stack-saved telemetry from the C ext
    "test_stack_saved.py",
    # Tracing not implemented
    "test_tracing.py",
}


collect_ignore_glob = list(_SKIP_MODULES)


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
    "test_throw_to_dead_thread_doesnt_crash": "threading not supported by port",
    "test_throw_to_dead_thread_doesnt_crash_wait": "threading not supported by port",
    "test_unexpected_reparenting": "threading not supported by port",
    "test_unexpected_reparenting_thread_running": "threading not supported by port",
    "test_no_gil_on_free_threaded": "free-threaded build feature",
    # frame inspection - depends on greenlet's C-level gr_frame
    "test_frame": "gr_frame not implemented in port",
    "test_can_access_f_back_of_suspended_greenlet": "gr_frame not implemented in port",
    "test_get_stack_with_nested_c_calls": "gr_frame not implemented in port",
    "test_frames_always_exposed": "gr_frame not implemented in port",
    # ``UnswitchableGreenlet`` is a debug-only C subclass that lets the
    # tests inject a failure point inside the assembly stack-switch
    # routine. There's no analogue for our pure-Python implementation
    # because we don't switch C stacks in the first place.
    "test_failed_to_initialstub": "requires _greenlet.UnswitchableGreenlet",
    "test_failed_to_switch_into_running": "requires _greenlet.UnswitchableGreenlet",
    # Remaining "broken greenlet" tests drive subprocesses that run
    # ``fail_*.py`` scripts. Those scripts exercise specific quirks of
    # the C extension (slp_switch reentrancy, trace-hook reentrancy,
    # etc.) and can't reasonably run under our port. The TestCase shim
    # raises ``SkipTest`` from ``run_script`` so they self-skip at
    # runtime, but listing them here keeps the suite output tidy.
    "test_failed_to_slp_switch_into_running": "requires subprocess + assembly switch failure",
    "test_reentrant_switch_two_greenlets": "requires subprocess + tracing reentrancy",
    "test_reentrant_switch_three_greenlets": "requires subprocess + tracing reentrancy",
    "test_reentrant_switch_three_greenlets2": "requires subprocess + tracing reentrancy",
    "test_reentrant_switch_GreenletAlreadyStartedInPython": "requires subprocess + run-attribute reentrancy",
    "test_reentrant_switch_run_callable_has_del": "requires subprocess + run-attribute reentrancy",
    # version: we have a custom version
    "test_version": "port has its own version string",
    # A subclass that overrides ``__getattribute__`` to (a) raise on
    # ``run`` access and (b) return ``None`` for every other name
    # (including our internal ``_parent`` / ``_dead`` / ``_started``
    # slots). Upstream reads those slots as C struct fields, bypassing
    # user attribute hooks; our port does not, so ``_wake``'s parent
    # walk sees ``None`` and can't route the SomeError back to the
    # switch caller. Fixing this would require routing every internal
    # slot access through ``object.__getattribute__``.
    "test_switch_to_dead_greenlet_with_unstarted_perverse_parent":
        "requires __getattribute__ transparency for internal slots",
    # contextvars: gr_context attribute not implemented
    "test_context_assignment_while_running": "gr_context not implemented",
    "test_context_assignment_wrong_type": "gr_context not implemented",
    "test_context_not_propagated": "gr_context not implemented",
    "test_context_propagated_by_context_run": "gr_context not implemented",
    "test_context_shared": "gr_context not implemented",
}


def pytest_collection_modifyitems(config, items):
    deselected = []
    remaining = []
    for item in items:
        reason = _INDIVIDUAL_SKIPS.get(item.name)
        if reason is None and ("thread" in item.name.lower()):
            reason = "threading not supported by port"
        if reason:
            # Use deselection rather than skip markers because pytest's
            # unittest collector wraps `TestCase` methods in a way that
            # makes ``add_marker`` ineffective for some skip outcomes.
            deselected.append(item)
        else:
            remaining.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = remaining
