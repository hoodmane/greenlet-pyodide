"""Basic greenlet tests for the Pyodide port.

These run inside a Pyodide interpreter via ``tests/run_tests.mjs``.
"""

import asyncio

import pytest

import greenlet
from greenlet import GreenletExit, getcurrent
from greenlet import greenlet as G

# ---------------------------------------------------------------------------
# module surface
# ---------------------------------------------------------------------------


def test_version_string():
    assert isinstance(greenlet.__version__, str)
    assert greenlet.__version__


def test_greenlet_exit_is_base_exception():
    assert issubclass(GreenletExit, BaseException)
    assert not issubclass(GreenletExit, Exception)


def test_getcurrent_returns_main_with_no_parent():
    main = getcurrent()
    assert isinstance(main, G)
    assert main.parent is None
    assert getcurrent() is main


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


def test_init_default_parent_is_current_greenlet():
    g = G(lambda: None)
    assert g.parent is getcurrent()


def test_init_explicit_none_parent_is_current_greenlet():
    g = G(parent=None)
    assert g.parent is getcurrent()


def test_init_run_can_be_none():
    g = G(run=None)
    assert g.run is None


def test_init_invalid_parent_type_raises():
    with pytest.raises(TypeError):
        G(lambda: None, parent="not a greenlet")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# core switching
# ---------------------------------------------------------------------------


def test_simple_ping_pong():
    log = []

    def f():
        log.append(1)
        getcurrent().parent.switch()
        log.append(3)

    g = G(f)
    log.append(0)
    g.switch()
    log.append(2)
    g.switch()
    log.append(4)
    assert log == [0, 1, 2, 3, 4]
    assert g.dead


def test_switch_passes_single_value_through():
    def f():
        v = getcurrent().parent.switch()
        getcurrent().parent.switch(v + 1)

    g = G(f)
    g.switch()
    assert g.switch(41) == 42


def test_switch_no_args_returns_empty_tuple():
    captured = []

    def f():
        captured.append(getcurrent().parent.switch())

    g = G(f)
    g.switch()
    g.switch()
    assert captured == [()]


def test_switch_multiple_args_returns_tuple():
    captured = []

    def f():
        captured.append(getcurrent().parent.switch())

    g = G(f)
    g.switch()
    g.switch(1, 2, 3)
    assert captured == [(1, 2, 3)]


def test_switch_kwargs_returns_args_kwargs_pair():
    captured = []

    def f():
        captured.append(getcurrent().parent.switch())

    g = G(f)
    g.switch()
    g.switch(1, 2, k=9)
    assert captured == [((1, 2), {"k": 9})]


def test_switch_kwargs_only_returns_kwargs_dict():
    captured = []

    def f():
        captured.append(getcurrent().parent.switch())

    g = G(f)
    g.switch()
    g.switch(k=9)
    assert captured == [{"k": 9}]


def test_first_switch_args_go_to_run():
    seen = []

    def f(a, b, *, k):
        seen.append((a, b, k))

    g = G(f)
    g.switch(1, 2, k=3)
    assert seen == [(1, 2, 3)]
    assert g.dead


def test_run_return_value_comes_back_to_parent():
    g = G(lambda: 99)
    assert g.switch() == 99
    assert g.dead


def test_reswitch_to_dead_greenlet_falls_through_to_self():
    g = G(lambda: "done")
    assert g.switch() == "done"
    # Switching into a dead greenlet falls through to the nearest live
    # ancestor, which is `main` (the caller). Since the live ancestor
    # is the source greenlet, this is effectively a self-switch and
    # the payload comes back as the return value.
    assert g.switch("ignored") == "ignored"


# ---------------------------------------------------------------------------
# getcurrent updates
# ---------------------------------------------------------------------------


def test_getcurrent_tracks_running_greenlet():
    seen = []

    def f():
        seen.append(getcurrent())
        getcurrent().parent.switch()
        seen.append(getcurrent())

    g = G(f)
    main = getcurrent()
    g.switch()
    assert seen[0] is g
    assert getcurrent() is main
    g.switch()
    assert seen[1] is g
    assert getcurrent() is main
    assert g.dead


# ---------------------------------------------------------------------------
# multiple / nested greenlets
# ---------------------------------------------------------------------------


def test_two_independent_children():
    log = []

    def f():
        log.append("x")
        getcurrent().parent.switch()
        log.extend(["x", "x"])

    g = G(f)
    h = G(f)
    g.switch()
    assert len(log) == 1
    h.switch()
    assert len(log) == 2
    h.switch()
    assert len(log) == 4
    assert h.dead
    g.switch()
    assert len(log) == 6
    assert g.dead


def test_nested_greenlets():
    log = []

    def inner():
        log.append("b")
        getcurrent().parent.switch()
        log.append("d")

    def outer():
        log.append("a")
        gi = G(inner)
        gi.switch()
        log.append("c")
        gi.switch()
        log.append("e")

    G(outer).switch()
    assert log == ["a", "b", "c", "d", "e"]


# ---------------------------------------------------------------------------
# exceptions and throw
# ---------------------------------------------------------------------------


def test_body_exception_propagates_to_parent_on_completion():
    class Boom(Exception):
        pass

    def f():
        raise Boom("kaboom")

    g = G(f)
    with pytest.raises(Boom, match="kaboom"):
        g.switch()
    assert g.dead


def test_throw_exception_class_with_arg():
    seen = []

    def f():
        try:
            getcurrent().parent.switch()
        except KeyError as e:
            seen.append(e)
            getcurrent().parent.switch("recovered")

    g = G(f)
    g.switch()
    assert g.throw(KeyError, "key") == "recovered"
    assert isinstance(seen[0], KeyError)
    assert seen[0].args == ("key",)


def test_throw_exception_instance():
    captured = []

    def f():
        try:
            getcurrent().parent.switch()
        except RuntimeError as e:
            captured.append(e)
            raise

    g = G(f)
    g.switch()
    exc = RuntimeError("hi")
    with pytest.raises(RuntimeError) as info:
        g.throw(exc)
    assert info.value is exc
    assert captured[0] is exc


def test_throw_greenlet_exit_returns_quietly():
    def f():
        getcurrent().parent.switch()

    g = G(f)
    g.switch()
    out = g.throw()  # default GreenletExit
    assert isinstance(out, GreenletExit)
    assert g.dead


def test_throw_into_unstarted_greenlet_kills_without_running():
    ran = []

    def f():
        ran.append(True)

    g = G(f)
    with pytest.raises(ValueError):
        g.throw(ValueError("nope"))
    assert ran == []
    assert g.dead


# ---------------------------------------------------------------------------
# parent semantics
# ---------------------------------------------------------------------------


def test_completion_returns_to_live_ancestor_skipping_dead_parents():
    log = []

    def grand():
        log.append("g")
        return "done"

    def child():
        log.append("c")
        gg = G(grand)
        gg.parent = getcurrent()
        gg.switch()
        log.append("c-after")
        return "child-ret"

    assert G(child).switch() == "child-ret"
    assert log == ["c", "g", "c-after"]


def test_parent_setter_rejects_non_greenlet():
    g = G(lambda: None)
    with pytest.raises(TypeError):
        g.parent = 42  # type: ignore[assignment]


def test_parent_setter_rejects_cycles():
    a = G(lambda: None)
    b = G(lambda: None, parent=a)
    with pytest.raises(ValueError):
        a.parent = b


def test_main_greenlet_parent_is_immutable():
    main = getcurrent()
    other = G(lambda: None)
    with pytest.raises(AttributeError):
        main.parent = other


# ---------------------------------------------------------------------------
# `run` attribute behavior
# ---------------------------------------------------------------------------


def test_switching_with_no_run_set_raises():
    g = G()
    with pytest.raises(AttributeError, match="run"):
        g.switch()


def test_run_is_hidden_after_start_and_cannot_be_reset():
    g = G(lambda: 1)
    g.switch()
    assert g.dead
    # ``run`` becomes inaccessible once the greenlet has started, and
    # remains so even after death.
    with pytest.raises(AttributeError, match="run"):
        _ = g.run
    with pytest.raises(AttributeError, match="run"):
        g.run = lambda: 2


def test_run_visible_before_first_switch():
    def f():
        return 1

    g = G(f)
    assert g.run is f
    g.run = lambda: 2  # still settable before start
    assert g.switch() == 2


# ---------------------------------------------------------------------------
# self-switch
# ---------------------------------------------------------------------------


def test_self_switch_returns_value_immediately():
    main = getcurrent()
    assert main.switch(7) == 7
    assert main.switch(1, 2) == (1, 2)
    assert main.switch() == ()


# ---------------------------------------------------------------------------
# interop with run_sync (Pyodide-only)
# ---------------------------------------------------------------------------


@pytest.mark.pyodide
def test_greenlet_body_can_use_run_sync():
    from pyodide.ffi import run_sync

    out = []

    def f():
        run_sync(asyncio.sleep(0))
        out.append("after-sleep-1")
        getcurrent().parent.switch()
        run_sync(asyncio.sleep(0))
        out.append("after-sleep-2")
        return "done"

    g = G(f)
    g.switch()
    assert out == ["after-sleep-1"]
    assert g.switch() == "done"
    assert out == ["after-sleep-1", "after-sleep-2"]


@pytest.mark.pyodide
def test_main_can_use_run_sync_between_switches():
    from pyodide.ffi import run_sync

    def f():
        getcurrent().parent.switch("A")
        getcurrent().parent.switch("B")

    g = G(f)
    a = g.switch()
    run_sync(asyncio.sleep(0))
    b = g.switch()
    assert (a, b) == ("A", "B")


# ---------------------------------------------------------------------------
# stress
# ---------------------------------------------------------------------------


def test_many_ping_pongs():
    def f():
        v = 0
        while True:
            v = getcurrent().parent.switch(v + 1)

    g = G(f)
    g.switch()
    val = 0
    for _ in range(50):
        val = g.switch(val)
    assert val == 50
