# -*- coding: utf-8 -*-
"""
greenlet for Pyodide.

A pure-Python re-implementation of the greenlet API on top of
:func:`pyodide.ffi.run_sync`.

How it works
============

In CPython, greenlet uses platform-specific assembly to swap C stacks.
On WebAssembly there is no portable way to do that, but Pyodide exposes
JavaScript Promise Integration (JSPI) via :func:`pyodide.ffi.run_sync`,
which lets a synchronous Python frame *block* on an awaitable while the
JS event loop continues to run.

We exploit that as follows:

* Each greenlet's body runs as an :class:`asyncio.Task`. The body
  itself is plain synchronous Python code; the only points at which it
  yields control are calls to :meth:`greenlet.switch` (or
  :meth:`~greenlet.throw`).
* While a greenlet is suspended, it is parked on a per-greenlet
  :class:`asyncio.Future` via ``run_sync``. The wasm stack at that
  point is suspended by JSPI; the JS event loop keeps running.
* To switch from greenlet *A* to greenlet *B*, *A* resolves *B*'s
  resume future (or starts *B*'s task on the first switch) and then
  immediately calls ``run_sync`` on its own freshly-created future.
  ``run_sync`` returns once another greenlet later resolves *A*'s
  future.

The "main" greenlet is implicit: the very first call into greenlet
machinery promotes the currently-executing call stack into a
``MainGreenlet`` instance that other greenlets descend from.

Limitations
-----------
* Threads are not supported (Pyodide is single-threaded). Each call to
  :func:`getcurrent` returns the single main greenlet.
* Tracing hooks (:func:`settrace`, :func:`gettrace`) are accepted but
  never invoked.
* The C-API capsule ``_C_API`` is not exported; native extensions
  linking against the greenlet C API will not work in Pyodide.
"""

from __future__ import annotations

import asyncio
import sys
import weakref
from typing import Any, Callable, Dict, Optional, Tuple

__all__ = [
    "__version__",
    "GreenletExit",
    "error",
    "getcurrent",
    "greenlet",
    "gettrace",
    "settrace",
    "GREENLET_USE_CONTEXT_VARS",
    "GREENLET_USE_GC",
    "GREENLET_USE_TRACING",
    "CLOCKS_PER_SEC",
    "enable_optional_cleanup",
    "get_clocks_used_doing_optional_cleanup",
]

__version__ = "3.5.1.dev0+pyodide"

# Compile-time-style flags. Greenlet exposes these so downstream code
# (e.g. gevent) can feature-detect.
GREENLET_USE_CONTEXT_VARS = True
GREENLET_USE_GC = True
GREENLET_USE_TRACING = False
CLOCKS_PER_SEC = 1_000_000


def enable_optional_cleanup(enabled: bool) -> None:  # noqa: D401
    """No-op shim for upstream API compatibility."""


def get_clocks_used_doing_optional_cleanup() -> int:
    return 0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GreenletExit(BaseException):
    """Raised inside a greenlet to terminate it.

    Inherits from :class:`BaseException` so a bare ``except Exception:``
    in user code does not silently swallow a kill request.
    """


class error(Exception):
    """Internal greenlet error (e.g. invalid switch)."""


# ---------------------------------------------------------------------------
# run_sync import
# ---------------------------------------------------------------------------

try:
    from pyodide.ffi import run_sync as _run_sync  # type: ignore
except ImportError:  # pragma: no cover - allow import outside Pyodide
    def _run_sync(awaitable):  # type: ignore[no-redef]
        raise error(
            "greenlet (Pyodide port) requires pyodide.ffi.run_sync, "
            "which is only available inside Pyodide."
        )


# ---------------------------------------------------------------------------
# Module-level state
#
# Pyodide is single-threaded, so a module-level "current greenlet"
# pointer is safe and equivalent to thread-local state in upstream
# greenlet.
# ---------------------------------------------------------------------------

_current: "greenlet | None" = None
_main: "greenlet | None" = None


def _get_loop() -> asyncio.AbstractEventLoop:
    try:
        return asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


# Sentinel that travels with a wake-up to indicate the resumed greenlet
# should re-raise an exception rather than receive a value.
class _Throw:
    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


def _pack_switch_value(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Any:
    """Encode the value a paused switch() should return.

    Mirrors upstream greenlet (see ``operator<<=`` in
    ``TGreenlet.cpp``):

    * ``g.switch()``           -> ``()``
    * ``g.switch(x)``          -> ``x``
    * ``g.switch(a, b)``       -> ``(a, b)``
    * ``g.switch(a, b, k=1)``  -> ``((a, b), {'k': 1})``
    * ``g.switch(k=1)``        -> ``{'k': 1}``
    """
    if kwargs:
        if not args:
            return kwargs
        return (args, kwargs)
    if len(args) == 1:
        return args[0]
    return args


# ---------------------------------------------------------------------------
# greenlet
# ---------------------------------------------------------------------------


class greenlet:
    """A lightweight cooperative thread of execution.

    Mirrors the public surface of upstream ``greenlet.greenlet`` for the
    core switch/throw/parent API.
    """

    __slots__ = (
        "__weakref__",
        "__dict__",
        "_run",
        "_parent",
        "_started",
        "_dead",
        "_resume_future",
        "_pending_value",
        "_task",
        "_main",
        "_gr_frame",
    )

    def __init__(
        self,
        run: Optional[Callable[..., Any]] = None,
        parent: "greenlet | None" = None,
    ) -> None:
        self._run = run
        if parent is None:
            parent = getcurrent()
        elif not isinstance(parent, greenlet):
            raise TypeError("parent must be a greenlet")
        self._parent: "greenlet | None" = parent
        self._started = False
        self._dead = False
        self._resume_future: Optional[asyncio.Future] = None
        self._pending_value: Any = None
        self._task: Optional[asyncio.Task] = None
        self._main = False
        self._gr_frame = None

    # ---- repr ----------------------------------------------------------

    def __repr__(self) -> str:
        cls = type(self).__name__
        flags = []
        if self._main:
            flags.append("main")
        if self._dead:
            flags.append("dead")
        elif self._started:
            flags.append("suspended")
        else:
            flags.append("pending")
        if self is _current:
            flags.append("current")
        return f"<{cls} object at 0x{id(self):x} ({' '.join(flags)})>"

    # ---- properties ---------------------------------------------------

    @property
    def run(self) -> Optional[Callable[..., Any]]:
        # Upstream hides ``run`` once the greenlet has started, both
        # while alive and after death.
        if self._started:
            raise AttributeError("run")
        return self._run

    @run.setter
    def run(self, value: Callable[..., Any]) -> None:
        if self._started:
            raise AttributeError(
                "run cannot be set after the start of the greenlet"
            )
        self._run = value

    @property
    def parent(self) -> "greenlet | None":
        return self._parent

    @parent.setter
    def parent(self, value: "greenlet") -> None:
        if not isinstance(value, greenlet):
            # Match upstream's error message verbatim.
            raise TypeError(
                "GreenletChecker: Expected any type of greenlet, not "
                + type(value).__name__
            )
        if self._main:
            raise AttributeError(
                "cannot set the parent of a main greenlet"
            )
        # Detect cycles: walk the new parent chain.
        cur: "greenlet | None" = value
        while cur is not None:
            if cur is self:
                raise ValueError("cyclic parent chain")
            cur = cur._parent
        self._parent = value

    @parent.deleter
    def parent(self) -> None:
        # Upstream forbids deletion with this exact message.
        raise AttributeError("can't delete attribute")

    @property
    def dead(self) -> bool:
        return self._dead

    @property
    def gr_frame(self):
        return self._gr_frame

    def __bool__(self) -> bool:
        # Upstream: a greenlet is truthy iff it is started and not
        # dead (i.e. currently active or suspended).
        return self._started and not self._dead

    # ---- copy / pickle protocol --------------------------------------

    # Greenlets are not safely copyable: their state involves running
    # frames, asyncio tasks, and JSPI suspenders. Reject copy/deepcopy
    # explicitly to match upstream.
    def __copy__(self):
        raise TypeError("greenlet objects are not copyable")

    def __deepcopy__(self, memo):
        raise TypeError("greenlet objects are not copyable")

    def __reduce__(self):
        raise TypeError("greenlet objects are not picklable")

    # ---- core operations ----------------------------------------------

    @staticmethod
    def getcurrent() -> "greenlet":
        """Class-method form of :func:`greenlet.getcurrent`."""
        return getcurrent()

    def switch(self, *args: Any, **kwargs: Any) -> Any:
        """Switch execution to this greenlet, returning when control comes back."""
        return _switch_to(self, args, kwargs, throw=None)

    def throw(
        self,
        typ: Any = GreenletExit,
        val: Any = None,
        tb: Any = None,
    ) -> Any:
        """Switch to this greenlet and raise an exception inside it."""
        import types as _types

        if isinstance(typ, BaseException):
            if val is not None:
                raise TypeError(
                    "instance exception may not have a separate value"
                )
            exc = typ
        elif isinstance(typ, type) and issubclass(typ, BaseException):
            if val is None:
                exc = typ()
            elif isinstance(val, tuple):
                exc = typ(*val)
            elif isinstance(val, BaseException):
                exc = val
            else:
                exc = typ(val)
        else:
            raise TypeError(
                "exceptions must be classes, or instances, not "
                + type(typ).__name__
            )
        if tb is not None:
            if not isinstance(tb, _types.TracebackType):
                raise TypeError(
                    "throw() third argument must be a traceback object"
                )
            exc = exc.with_traceback(tb)
        return _switch_to(self, (), {}, throw=exc)

    # ---- finalization --------------------------------------------------

    def __del__(self) -> None:
        # Upstream throws ``GreenletExit`` into a suspended greenlet at
        # finalization so its ``finally`` clauses run. We do the same
        # by switching into it with a GreenletExit. This only fires if
        # the greenlet was started, hasn't died yet, and the body's
        # frame released its self-reference (see :func:`_body`, which
        # holds a weakref).
        # If ``__init__`` raised before fully initialising, the slots
        # we need won't be set; bail out silently in that case.
        if not getattr(self, "_started", False):
            return
        if getattr(self, "_dead", True):
            return
        # Avoid running into our own destructor recursively.
        try:
            current = getcurrent()
        except Exception:  # pragma: no cover - shutdown
            return
        if current is self:
            return
        try:
            _switch_to(self, (), {}, throw=GreenletExit())
        except BaseException:  # noqa: BLE001 - swallow, like upstream
            # Any exception that escapes here is unraisable; route it
            # through ``sys.unraisablehook`` to match upstream.
            try:
                hook = sys.unraisablehook
            except AttributeError:  # pragma: no cover
                return

            class _Unraisable:
                __slots__ = ("exc_type", "exc_value", "exc_traceback",
                             "err_msg", "object")
            ur = _Unraisable()
            exc = sys.exc_info()
            ur.exc_type = exc[0]
            ur.exc_value = exc[1]
            ur.exc_traceback = exc[2]
            ur.err_msg = None
            ur.object = self
            try:
                hook(ur)
            except Exception:  # pragma: no cover
                pass


# ---------------------------------------------------------------------------
# Switching machinery
# ---------------------------------------------------------------------------


def _ensure_main() -> "greenlet":
    """Lazily promote the current call stack to the main greenlet."""
    global _main, _current
    if _main is None:
        m = greenlet.__new__(greenlet)
        m._run = None
        m._parent = None
        m._started = True
        m._dead = False
        m._resume_future = None
        m._pending_value = None
        m._task = None
        m._main = True
        m._gr_frame = None
        _main = m
        if _current is None:
            _current = m
    return _main


async def _body(
    self_ref: "weakref.ref[greenlet]",
    args: Tuple[Any, ...],
    kwargs: Dict[str, Any],
) -> None:
    """Drive the user-supplied ``run`` for a freshly-started greenlet.

    Held by the asyncio task only via a :func:`weakref.ref` so that the
    greenlet itself can be garbage-collected (and its ``__del__`` run)
    while it is suspended waiting on its resume future.

    Despite being ``async``, this coroutine never ``await``\\ s. The
    only suspension points are the ``run_sync`` calls inside
    :func:`_switch_to`.
    """
    self = self_ref()
    if self is None:
        return
    run = self._run
    # Don't keep a strong reference here either: the body is allowed to
    # outlive the greenlet object if the user's code holds onto a
    # closure that references it explicitly.
    del self
    result: Any = None
    exc: Optional[BaseException] = None
    try:
        if run is None:
            # An unstarted greenlet was woken to receive a "fall-off"
            # result from a child returning. Since there's no run
            # function, the child's return value is delivered as an
            # AttributeError up the parent chain.
            raise AttributeError("run")
        try:
            result = run(*args, **kwargs)
        except GreenletExit as e:
            # GreenletExit is delivered to the parent as a *value*,
            # matching upstream semantics.
            result = e
        except BaseException as e:  # noqa: BLE001 - re-raised via _wake
            exc = e
    except BaseException as e:  # noqa: BLE001
        exc = e
    finally:
        del run

    self = self_ref()
    if self is None:
        # Greenlet was finalized while running; nothing to do.
        return
    self._dead = True
    self._started = True
    self._run = None

    target = self._parent
    while target is not None and target._dead:
        target = target._parent
    if target is None:
        if exc is not None:
            sys.excepthook(type(exc), exc, exc.__traceback__)
        return
    # Body completion delivers ``result`` to the parent as if the dead
    # child had called ``parent.switch(result)``.
    _wake(target, args=(result,) if exc is None else (), exc=exc)


def _wake(
    target: "greenlet",
    args: Tuple[Any, ...] = (),
    kwargs: Optional[Dict[str, Any]] = None,
    exc: Optional[BaseException] = None,
) -> None:
    """Make ``target`` runnable with the given resume payload.

    For an unstarted greenlet, this schedules its body coroutine with
    ``run(*args, **kwargs)``. For an already-suspended greenlet, this
    completes the future it is parked on with the packed switch value
    so that its ``run_sync`` returns.
    """
    if kwargs is None:
        kwargs = {}

    if target._dead:
        # Switch through to a live ancestor instead.
        ancestor = target._parent
        while ancestor is not None and ancestor._dead:
            ancestor = ancestor._parent
        if ancestor is None:
            if exc is not None:
                raise exc
            return
        _wake(ancestor, args=args, kwargs=kwargs, exc=exc)
        return

    if not target._started:
        if exc is not None:
            # Throwing into an unstarted greenlet kills it without
            # running ``run`` at all; bounce up to the parent.
            target._started = True
            target._dead = True
            parent = target._parent
            while parent is not None and parent._dead:
                parent = parent._parent
            if parent is None:
                raise exc
            _wake(parent, exc=exc)
            return
        target._started = True
        loop = _get_loop()
        # Hold the body coroutine via a weakref so ``__del__`` can fire
        # while the greenlet is suspended and other references are
        # released.
        target._task = loop.create_task(_body(weakref.ref(target), args, kwargs))
        return

    # Already suspended. Stash the wake-up payload and resolve the
    # future so its ``run_sync`` returns.
    if exc is not None:
        target._pending_value = _Throw(exc)
    else:
        target._pending_value = _pack_switch_value(args, kwargs)
    fut = target._resume_future
    assert fut is not None, "suspended greenlet has no resume future"
    if not fut.done():
        fut.set_result(None)


def _switch_to(
    target: "greenlet",
    args: Tuple[Any, ...],
    kwargs: Dict[str, Any],
    throw: Optional[BaseException],
) -> Any:
    """Implementation of ``greenlet.switch`` / ``greenlet.throw``."""
    global _current

    _ensure_main()
    src = _current
    assert src is not None

    # Self-switch is a no-op that just delivers the payload.
    if target is src:
        if throw is not None:
            raise throw
        return _pack_switch_value(args, kwargs)

    # Switching to a dead greenlet falls through to the nearest live
    # ancestor (matches upstream).
    while target._dead:
        if target._parent is None:
            raise error("cannot switch to a dead greenlet with no parent")
        target = target._parent

    if target is src:
        # After dead-greenlet fall-through we landed back on ourselves.
        if throw is not None:
            # Upstream: throwing ``GreenletExit`` into an
            # already-dead greenlet is "eaten" - it returns the
            # exception as a value, mirroring an ordinary fall-off
            # exit. Other exception types still propagate.
            if isinstance(throw, GreenletExit):
                return throw
            raise throw
        return _pack_switch_value(args, kwargs)

    # Validate the target has something to run on its first switch.
    # Both ``switch`` and ``throw`` raise ``AttributeError`` here in
    # upstream when the target has no ``run``.
    if not target._started and target._run is None:
        raise AttributeError("run")

    # Allocate our own resume future *before* waking the target so that
    # if the target wakes us back up immediately we don't miss it.
    loop = _get_loop()
    src._resume_future = loop.create_future()
    my_future = src._resume_future

    # Wake the target with the appropriate payload.
    if throw is not None:
        _wake(target, exc=throw)
    else:
        _wake(target, args=args, kwargs=kwargs)

    # We are no longer the running greenlet.
    _current = target
    # Replace ``src`` and ``target`` with weakrefs so this suspended
    # frame does NOT keep the greenlets alive. The C-extension allows
    # a suspended greenlet to be GC'd (firing its destructor, which
    # throws ``GreenletExit`` into it); we preserve that by ensuring
    # the only strong references on the parked stack come from
    # framework-owned objects (asyncio task / loop), not our own code.
    src_ref = weakref.ref(src)
    del src, target

    # Park ourselves until somebody resolves my_future. ``run_sync``
    # suspends the wasm stack via JSPI; the JS event loop continues to
    # turn so other greenlets / asyncio tasks make progress.
    _run_sync(my_future)
    del my_future

    # We're back. Reinstate ourselves as the current greenlet and
    # consume the wake-up payload.
    src = src_ref()
    if src is None:
        # We were garbage-collected while parked. The destructor
        # already arranged the appropriate teardown; just unwind.
        return None
    _current = src
    payload = src._pending_value
    src._pending_value = None
    src._resume_future = None
    if isinstance(payload, _Throw):
        raise payload.exc
    return payload


# ---------------------------------------------------------------------------
# Module-level functions
# ---------------------------------------------------------------------------


def getcurrent() -> "greenlet":
    """Return the currently executing greenlet."""
    _ensure_main()
    assert _current is not None
    return _current


def settrace(_callback: Any) -> None:  # noqa: D401
    """Tracing is not implemented in the Pyodide port."""
    return None


def gettrace() -> None:
    return None


# Expose the exceptions on the class itself so that downstream code can
# write ``greenlet.greenlet.GreenletExit`` like in upstream.
greenlet.GreenletExit = GreenletExit  # type: ignore[attr-defined]
greenlet.error = error  # type: ignore[attr-defined]


# Compatibility alias used by some downstream code.
GreenletType = greenlet
