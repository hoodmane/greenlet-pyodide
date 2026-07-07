# -*- coding: utf-8 -*-
"""
greenlet for Pyodide.

A pure-Python re-implementation of the greenlet API on top of
:func:`pyodide.ffi.run_sync`.

How it works
============

In CPython, greenlet uses platform-specific assembly to swap C stacks. Pyodide
exposes JavaScript Promise Integration (JSPI) via :func:`pyodide.ffi.run_sync`,
which lets a synchronous Python frame *block* on an awaitable while the JS event
loop continues to run.

We exploit that as follows:

* Each greenlet's body runs as an :class:`asyncio.Task`. The body itself is
  plain synchronous Python code; the only points at which it yields control are
  calls to :meth:`greenlet.switch` (or :meth:`~greenlet.throw`).
* While a greenlet is suspended, it is parked on a per-greenlet
  :class:`asyncio.Future` via ``run_sync``. The wasm stack at that point is
  suspended by JSPI; the JS event loop keeps running.
* To switch from greenlet *A* to greenlet *B*, *A* resolves *B*'s resume future
  (or starts *B*'s task on the first switch) and then immediately calls
  ``run_sync`` on its own freshly-created future. ``run_sync`` returns once
  another greenlet later resolves *A*'s future.

The "main" greenlet is implicit: the very first call into greenlet machinery
promotes the currently-executing call stack into a ``MainGreenlet`` instance
that other greenlets descend from.

Limitations
-----------
* Tracing hooks (:func:`settrace`, :func:`gettrace`) are accepted but never
  invoked.
* The C-API capsule ``_C_API`` is not exported; native extensions linking
  against the greenlet C API will not work in Pyodide.
"""

from __future__ import annotations

import asyncio
import gc
import sys
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from pyodide.ffi import run_sync as _run_sync  # type: ignore

# Tracks whether a cycle-collection pass is currently in progress.
# Populated via ``gc.callbacks`` below. ``__del__`` consults this to
# avoid re-entering the greenlet switching machinery from cycle GC:
# JSPI's ``run_sync`` returns spuriously when nested inside a cycle
# collection pass, which would otherwise deadlock finalization.

class _GCProbe:
    gc_active: bool = False

    @classmethod
    def callback(cls, phase: str, _info: dict) -> None:  # noqa: ARG001
        if phase == "start":
            cls.gc_active = True
        elif phase == "stop":
            cls.gc_active = False


gc.callbacks.append(_GCProbe.callback)

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
@dataclass(slots=True)
class _Throw:
    exc: BaseException


# Structural type passed to ``sys.unraisablehook`` when an exception
# escapes from within a greenlet's ``__del__``. Matches the attribute
# surface CPython's own C-level unraisable machinery constructs
# (``exc_type``, ``exc_value``, ``exc_traceback``, ``err_msg``,
# ``object``). Constructed inside an ``except`` block: pulls the
# in-flight exception out of ``sys.exc_info()``.
class _Unraisable:
    __slots__ = ("exc_type", "exc_value", "exc_traceback", "err_msg", "object")

    def __init__(self, obj: Any, err_msg: Optional[str] = None) -> None:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        self.exc_type = exc_type
        self.exc_value = exc_value
        self.exc_traceback = exc_traceback
        self.err_msg = err_msg
        self.object = obj


def _resolve_run(target: "greenlet") -> Optional[Callable[..., Any]]:
    """Look up the callable to run for a freshly-starting greenlet.

    Upstream reads the ``run`` attribute at the first switch via normal
    Python attribute access -- that means subclass ``__getattribute__``
    hooks run and can have side effects (e.g. recursively switching
    into the greenlet before it has officially "started"). We do the
    same by calling :func:`getattr` on ``target``. If ``run`` is
    absent (raises :class:`AttributeError`) we return ``None`` so the
    caller can raise ``AttributeError("run")`` at the switch site,
    matching upstream.

    The base-class ``run`` property returns ``self._run`` when the
    greenlet has not yet started, so ``getattr(target, 'run')``
    naturally picks up:

    * a ``run=`` keyword passed to ``__init__`` (stored in ``_run``),
    * a subclass override that shadows the property (method / attr),
    * anything a subclass ``__getattribute__`` chooses to return.
    """
    try:
        result = getattr(target, "run")
    except AttributeError:
        return None
    if result is None:
        return None
    return result


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

    def __new__(cls, *args: Any, **kwargs: Any) -> "greenlet":
        # Initialise all slots in ``__new__`` so that subclasses that
        # override ``__init__`` without calling ``super().__init__``
        # still get a fully-formed instance. Upstream does the same in
        # ``tp_new``. ``__init__`` may later overwrite ``_run`` and
        # ``_parent`` from user-supplied arguments.
        self = super().__new__(cls)
        self._run = None
        # Default parent is the current greenlet at instantiation
        # time. Skip the lazy-main promotion when we're constructing
        # the main greenlet itself (see :func:`_ensure_main`).
        if cls is greenlet and _main is None:
            self._parent = None
        else:
            self._parent = _ensure_main() if _current is None else _current
        self._started = False
        self._dead = False
        self._resume_future = None
        self._pending_value = None
        self._task = None
        self._main = False
        self._gr_frame = None
        return self

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
        self._parent = parent

    # ---- attribute deletion --------------------------------------------

    def __delattr__(self, name: str) -> None:
        # Upstream forbids deletion of ``__dict__`` (the instance
        # attribute-storage dict) with a ``TypeError``. Match that.
        if name == "__dict__":
            raise TypeError("__dict__ may not be deleted")
        super().__delattr__(name)

    # ---- repr ----------------------------------------------------------

    def __repr__(self) -> str:
        # Mirror upstream ``green_repr`` in ``PyGreenlet.cpp``. Format:
        #   Alive:  <TYPE object at 0xADDR (otid=0xTID)[ current| suspended][ active][ pending| started][ main]>
        #   Dead:   <TYPE object at 0xADDR (otid=0xTID) dead>
        # We are single-threaded, so ``otid`` is fixed at 0.
        cls = type(self).__module__ + "." + type(self).__qualname__
        addr = id(self)
        tid = 0
        if self._dead:
            return f"<{cls} object at 0x{addr:x} (otid=0x{tid:x}) dead>"
        never_started = not self._started
        parts = [f"<{cls} object at 0x{addr:x} (otid=0x{tid:x})"]
        if self is _current:
            parts.append(" current")
        elif self._started:
            parts.append(" suspended")
        # "active" == started and not dead. We've already ruled out
        # dead above, so this is equivalent to ``self._started``.
        if self._started:
            parts.append(" active")
        parts.append(" pending" if never_started else " started")
        if self._main:
            parts.append(" main")
        parts.append(">")
        return "".join(parts)

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
        # Switch into task with a ``GreenletExit`` if we can. This only fires if
        # the greenlet was started, hasn't died yet, and the body's frame
        # released its self-reference.
        if not getattr(self, "_started", False):
            return
        if getattr(self, "_dead", True):
            return
        # When ``__del__`` is invoked from Python's cycle GC pass, our
        # ``_switch_to`` machinery cannot yield. Rather than deadlock, simply
        # mark the greenlet dead and skip the switch. This means the greenlet's
        # ``finally`` clauses will NOT run for cycle-collected greenlets. This
        # actually happens in upstream greenlet too.
        #
        # Note: it seems like can_run_sync() might be appropriate here instead
        # but it isn't.
        if _GCProbe.gc_active or sys.is_finalizing():
            self._dead = True
            self._started = True
            self._run = None
            self._task = None
            return
        # Avoid running into our own destructor recursively.
        try:
            current = getcurrent()
        except Exception:
            return
        if current is self:
            return
        # Temporarily reparents the dying greenlet to the the current greenlet
        # for the duration of the kill, then restores the original parent once
        # the body has exited. That way the GreenletExit handler observes the
        # current greenlet as its parent and any unhandled exceptions propagate
        # to the current greenlet rather than the original parent.
        original_parent = self._parent
        if current is not original_parent:
            try:
                self._parent = current
            except (AttributeError, ValueError, TypeError):
                # Reparenting can fail (e.g. a cycle); if so, fall
                # back to the original parent and proceed.
                original_parent = None
        try:
            try:
                _switch_to(self, (), {}, throw=GreenletExit())
            except BaseException:  # noqa: BLE001 - swallow
                # Any exception that escapes here is unraisable; route
                # it through ``sys.unraisablehook``
                try:
                    hook = sys.unraisablehook
                except AttributeError:
                    return
                try:
                    hook(_Unraisable(self))
                except Exception:
                    pass
        finally:
            if original_parent is not None:
                # Restore the original parent now that the body has
                # finished executing.
                self._parent = original_parent


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
    run: Optional[Callable[..., Any]],
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
    global _current
    self = self_ref()
    if self is None:
        return
    # We are the greenlet whose wasm stack is now executing Python code.
    # ``_switch_to`` deliberately leaves ``_current`` pointing at the
    # suspending greenlet until the resuming one takes over here.
    _current = self
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
        # Resolve ``run`` via normal attribute lookup. This honors any
        # subclass ``__getattribute__`` hook and may re-enter our
        # switching machinery. If it returns ``None`` (no ``run``),
        # ``_body`` raises ``AttributeError("run")`` and delivers it
        # to the parent.
        run = _resolve_run(target)
        target._started = True
        loop = _get_loop()
        # Hold the body coroutine via a weakref so ``__del__`` can fire
        # while the greenlet is suspended and other references are
        # released. Pass the pre-resolved ``run`` in: our ``run``
        # property raises ``AttributeError`` once ``_started`` is set,
        # so ``_body`` cannot re-resolve after this point.
        target._task = loop.create_task(_body(weakref.ref(target), run, args, kwargs))
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
    if not target._started and _resolve_run(target) is None:
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

    # NOTE: ``_current`` is intentionally NOT updated here. ``_current``
    # names the greenlet whose wasm stack is currently executing Python
    # code. We are still on ``src``'s wasm stack until ``_run_sync``
    # actually yields it, so ``src`` must remain current. The resuming
    # greenlet is responsible for updating ``_current`` when its wasm
    # stack takes over (either in :func:`_body` for a fresh start, or
    # in :func:`_switch_to` post-``_run_sync`` for a resumed suspend).
    #
    # This matters because a destructor firing between the ``_wake``
    # call and ``_run_sync`` (e.g. via ``del src`` dropping the last
    # reference to a suspended greenlet other than ``src``) will call
    # :func:`getcurrent`; that must still report ``src`` so the
    # destructor's ``current is self`` guard behaves as upstream.
    #
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

    src = src_ref()
    if src is None:
        # We were garbage-collected while parked. The destructor
        # already arranged the appropriate teardown; just unwind.
        return None
    # We're back. Consume the wake-up payload BEFORE updating
    # ``_current``. Reassigning ``_current`` decrefs the outgoing
    # value; if that triggers a destructor (upstream: throwing
    # ``GreenletExit`` into a suspended greenlet), the destructor
    # may reenter :func:`_switch_to` recursively and clobber our
    # ``_pending_value``. Snapshot the payload first, and clear the
    # slots before touching ``_current`` so the recursive path sees
    # a clean state.
    payload = src._pending_value
    src._pending_value = None
    src._resume_future = None
    _current = src
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
