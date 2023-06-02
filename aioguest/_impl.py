from __future__ import annotations

# asyncio "guest mode": run asyncio in the same thread as another event loop.
# This is closely modeled after Trio's guest mode, and you should read about
# it to understand what we're doing:
#     https://trio.readthedocs.io/en/stable/reference-lowlevel.html#using-guest-mode-to-run-trio-on-top-of-other-event-loops
#     and the implementation in trio/_core/_run.py
# The only public function here is start_guest_run().
#
# Our version has more monkeypatching, since we don't have asyncio's
# cooperation.  The monkeypatching generally works like so:
# - Suppose we want to interpose some logic in a Foo object. (Foo might be
#   an event loop type, or a selector type, etc.)
# - Define a class FooOverrides containing the interposed logic. Its methods
#   can use super() to invoke the original Foo ones.
# - Dynamically create a class FooWithOverrides(FooOverrides, Foo).
# - Use __class__ assignment to change the type of the relevant Foo to
#   FooWithOverrides.
# This has the advantage that it only affects the specific event loop
# instance we're trying to run in guest mode; there's no spillover into
# other threads.
#
# Since asyncio did not choose to implement their event loop as a generator,
# we use greenlets instead. In combination with the monkeypatching, this
# allows us to defer just the selector.select() call, or just
# the GetQueuedCompletionStatus call on Windows, to a thread (and allow
# the host event loop to run while we're waiting on it). This provides
# comparable thread-safety benefits to Trio's IO managers' split betwen
# get_events() and process_events().
#
# We currently depend on Trio solely for the start_thread_soon thread cache.
# The host loop does not need to be Trio.

import asyncio
import attrs
import greenlet
import itertools
import logging
import selectors
import signal
import sys
import threading
import types
import warnings
import weakref
from trio.lowlevel import start_thread_soon
from sniffio import thread_local as sniffio_loop
from functools import partial
from outcome import Outcome, Value, Error, capture
from typing import Optional, Callable, Any


log = logging.getLogger("aioguest")


def report_abandoned(result):
    try:
        log.warning(
            "Host loop abandoned us so we couldn't deliver the guest result: "
            + repr(result.unwrap())
        )
    except BaseException:
        log.exception(
            "Host loop abandoned us so we couldn't deliver the guest exception:"
        )


def yield_to_host(request):
    """Yield control to the host loop, passing *request* to guest_tick().
    It may be either None (to request that we be rescheduled soon with None) or
    a callable (to request that the callable be run in a worker thread, and
    we be rescheduled with its outcome once it completes).

    If the host event loop exits without resuming us, then GuestState will
    (probably) be GC'ed and thus so will our greenlet, resulting in a
    GreenletExit exception being raised here. We attempt to handle this
    by removing our monkeypatching so the rest of the run can complete
    synchronously. The GreenletExit exception will propagate out of run(),
    stopping the event loop.
    """
    # NB: it's important not to save the result of greenlet.getcurrent() here
    # because it would interfere with the logic below that notices if the
    # greenlet gets abandoned
    parent = greenlet.getcurrent().parent
    try:
        return parent.switch(request)
    except greenlet.GreenletExit:
        warnings.warn(
            RuntimeWarning(
                "asyncio guest run got abandoned without properly finishing... "
                "weird stuff might happen"
            )
        )
        gr = greenlet.getcurrent()
        gr.unpatch()  # now it's a normal non-guest asyncio loop

        # On CPython our actual parent isn't going to see our result,
        # so make sure someone else does. (On PyPy it doesn't work to
        # set a not-yet-started greenlet as your parent, but we also
        # don't need to because our outcome will flow through to
        # GuestState.__del__.)
        gr.parent = greenlet.greenlet(report_abandoned, parent=gr.parent)

        # Attempt to cause the aio loop to exit soon
        msg = lambda s: (s,) if sys.version_info >= (3, 9) else ()
        if gr.main_task is not None and not gr.main_task.done():
            gr.main_task.cancel(
                *msg("aioguest cancelling main task due to abandonment by host loop")
            )
        else:
            for task in asyncio.all_tasks():
                task.cancel(
                    *msg(
                        "aioguest cancelling all tasks due to abandonment by host loop with "
                        "no active main task"
                    )
                )
        return getattr(gr, "thread_outcome", None)


# Overrides for any selectors.BaseSelector that uses a thread-safe
# (kernel-based) mechanism for tracking interest: kqueue, epoll, or devpoll.
class SelectorOverrides:
    def select(self, timeout=None):
        # Avoid the worker thread if possible: if we weren't asked to wait,
        # or if any I/O was immediately ready, then just do a brief yield.
        immediate_result = super().select(0)
        if immediate_result or (timeout is not None and timeout <= 0):
            yield_to_host(None)
            return immediate_result
        # Otherwise defer the actual waiting to a thread.
        return yield_to_host(partial(super().select, timeout)).unwrap()


# For Windows support, our patched asyncio.IocpProactor evaluates the original
# _poll() method with its globals()["_overlapped"] overridden to be an instance
# of PatchedOverlappedModule. Thus, we effectively patch the _overlapped module,
# but only from the perspective of that specific IocpProactor instance.
class PatchedOverlappedModule:
    def GetQueuedCompletionStatus(self, iocp, timeout_ms):
        from _overlapped import GetQueuedCompletionStatus as original

        if timeout_ms == 0:
            return original(iocp, 0)
        # Defer nontrivial-timeout GetQueuedCompletionStatus calls to a
        # background thread. Each call to _poll() will only make one such
        # call, its first one; further calls set timeout_ms to zero.
        return yield_to_host(partial(original, iocp, timeout_ms)).unwrap()

    # Forward all other attribute accesses to the underlying module

    def __getattr__(self, name):
        import _overlapped

        return getattr(_overlapped, name)

    def __setattr__(self, name, value):  # pragma: no cover
        raise AttributeError("unexpected attribute assignment")


# Overrides for asyncio.IocpProactor
class ProactorOverrides:
    def select(self, timeout=None):
        # Avoid the worker thread if possible: if we weren't asked to wait,
        # or if any I/O was immediately ready, then just do a brief yield.
        immediate_result = super().select(0)
        if immediate_result or (timeout is not None and timeout <= 0):
            yield_to_host(None)
            return immediate_result
        # Otherwise the nonzero-timeout event wait in _poll() will get
        # deferred to a thread.
        return super().select(timeout)

    def _poll(self, timeout=None):
        # This only runs the first time _poll is called; it replaces
        # the definition of ProactorOverrides._poll.
        # Evaluate IocpProactor._poll() with our patched _overlapped module.
        underlying = super()._poll.__func__
        wrapper = ProactorOverrides._poll
        new_func = types.FunctionType(
            underlying.__code__,
            {**underlying.__globals__, "_overlapped": PatchedOverlappedModule()},
            wrapper.__name__,
            underlying.__defaults__,
            underlying.__closure__,
        )
        new_func.__qualname__ = wrapper.__qualname__
        setattr(ProactorOverrides, "_poll", new_func)
        return self._poll(timeout)


# Overrides for asyncio.BaseEventLoop to wake up if callbacks/timeouts
# are relevantly added by the host loop. This is analogous to the 'if is_guest:'
# conditions in trio._core._run.Runner.
class EventLoopOverrides:
    def call_at(self, when, callback, *args, context=None):
        is_earliest = not self._scheduled or when < self._scheduled[0]._when
        try:
            return super().call_at(when, callback, *args, context=context)
        finally:
            if is_earliest and not self._aioguest_guest_tick_scheduled:
                self._aioguest_guest_tick_scheduled = True
                self._write_to_self()

    def call_soon(self, callback, *args, context=None):
        was_first = not self._ready
        try:
            return super().call_soon(callback, *args, context=context)
        finally:
            if was_first and not self._aioguest_guest_tick_scheduled:
                self._aioguest_guest_tick_scheduled = True
                self._write_to_self()

    # NB: _add_callback also appends to self._ready, but it's only called from
    # the event loop itself (to schedule FD reader/writer or signal callbacks)
    # so it doesn't need to be able to wake us up

    # Attempt to finalize asyncio async generators in asyncio, without
    # taking over async generators that belong to the host loop.
    # See trio/_core/_asyncgens.py for more details on this approach.
    def _asyncgen_firstiter_hook(self, agen):
        if sniffio_loop.name == "asyncio":
            return super()._asyncgen_firstiter_hook(agen)
        agen.ag_frame.f_locals["@aioguest_foreign_asyncgen"] = True
        if self._aioguest_prev_asyncgen_hooks.firstiter is not None:
            self._aioguest_prev_asyncgen_hooks.firstiter(agen)

    def _asyncgen_finalizer_hook(self, agen):
        try:
            is_ours = not agen.ag_frame.f_locals.get("@aioguest_foreign_asyncgen")
        except AttributeError:  # pragma: no cover
            is_ours = True
        if is_ours:
            return super()._asyncgen_finalizer_hook(agen)

        # Not ours; deliver to host loop's asyncgen finalizer.
        if self._aioguest_prev_asyncgen_hooks.finalizer is not None:
            return self._aioguest_prev_asyncgen_hooks.finalizer(agen)

        # Host has no finalizer.  Reimplement the default Python
        # behavior with no hooks installed: throw in GeneratorExit,
        # step once, raise RuntimeError if it doesn't exit.
        closer = agen.aclose()
        try:
            # If the next thing is a yield, this will raise RuntimeError
            # which we allow to propagate
            closer.send(None)
        except StopIteration:
            pass
        else:
            # If the next thing is an await, we get here. Give a nicer
            # error than the default "async generator ignored GeneratorExit"
            raise RuntimeError(
                f"Non-asyncio async generator {agen!r} awaited something "
                "during finalization; install a finalization hook to "
                "support this, or wrap it in 'async with aclosing(...):'"
            )


def patch_one(obj, overrides, undo_list, *, _cache={}):
    """Apply monkeypatches in the class *overrides* to the object *obj*.
    This is done by creating a new class that inherits from both
    *overrides* and ``type(obj)``, and using ``__class__`` assignment
    to change *obj*'s type to the new class. Appends a tuple
    ``(obj, original_type)`` to *undo_list* so that the patching
    can be undone later.
    """
    ty = type(obj)
    try:
        patched = _cache[ty]
    except KeyError:
        for disambiguator in itertools.count():  # pragma: no branch
            name = f"{ty.__name__}{disambiguator or ''}+aioguest"
            if not hasattr(globals(), name):  # pragma: no branch
                break
        patched = _cache[ty] = type(
            name,
            (overrides, ty),
            {"__module__": __name__, "__name__": name, "__qualname__": name},
        )
        globals()[name] = patched

    obj.__class__ = patched
    undo_list.append((obj, ty))


def unpatch(undo_list):
    """Undo the effects of one or more calls to patch()."""
    while undo_list:
        obj, orig_type = undo_list[-1]
        obj.__class__ = orig_type
        undo_list.pop()


class CheckForZombieGreenlets:
    """Logic for trying to cause another thread to execute
    greenlet.getcurrent() in order to finalize an aioguest greenlet
    that got collected on a different thread. See GuestState.__del__.
    Note that this is only used on CPython, where it is fairly likely
    that the last reference to an abandoned GuestState will be dropped
    from the I/O worker thread. (PyPy has the same problem in theory,
    but GC is more likely to run on a thread that's doing work, which
    the I/O thread isn't after abandonment.)
    """

    class WhenBoolChecked:
        def __bool__(self):
            greenlet.getcurrent()
            return False

    when_bool_checked = WhenBoolChecked()

    @attrs.define
    class DuringGC:
        """Create a GC cycle and call greenlet.getcurrent() when it is collected.
        Repeat this process with one attempt per GC until `ref` is no longer alive
        or we've tried too many times.
        """

        ref: weakref.ref
        count: int = 0
        cycle: DuringGC = attrs.Factory(lambda self: self, takes_self=True)

        def __del__(self):
            greenlet.getcurrent()
            if self.ref() is not None:  # pragma: no cover
                if self.count >= 5:
                    warnings.warn(
                        RuntimeWarning(
                            "asyncio guest run got abandoned without properly finishing, "
                            "and we couldn't get its thread to try to finish it. "
                            "Weird stuff will probably happen."
                        )
                    )
                else:
                    # try again next GC cycle
                    type(self)(ref=self.ref, count=self.count + 1)

    @staticmethod
    def soon(gr_ref):
        # The ways to interrupt another thread in Python without its
        # cooperation are pretty limited:
        # - Raise a signal. Only works when targeting the main thread,
        #   and requires that you set up a signal handler in advance,
        #   which might interfere with something the application is doing.
        # - Py_AddPendingCall(). Only works when targeting the main thread,
        #   and has a limited C-level API. We'll use this for the main thread.
        # - PyThreadState_SetAsyncExc(). Can target any thread, but only
        #   by making it raise an exception, which we don't want to do here.
        # - Put something in a finalizer reachable from a GC cycle and hope
        #   that it gets collected on the right thread. We'll use this for
        #   non-main threads, but it's definitely not guaranteed to work.
        import ctypes

        # PyObject_IsTrue has the right signature for AddPendingCall.
        # The __bool__ being called must return zero to indicate that
        # the pending call is not raising an exception. A reference is
        # not conveyed along with the call so we must use a global object.
        ctypes.pythonapi.Py_AddPendingCall(
            ctypes.cast(ctypes.pythonapi.PyObject_IsTrue, ctypes.c_void_p),
            ctypes.cast(id(CheckForZombieGreenlets.when_bool_checked), ctypes.c_void_p),
        )

        # If the pending call doesn't work, hopefully we can observe a GC
        # on the correct thread; we don't really have any other options.
        CheckForZombieGreenlets.DuringGC(gr_ref)


@attrs.define(eq=False)
class GuestState:
    loop: asyncio.BaseEventLoop

    # The value that was passed to signal.set_wakeup_fd() before the
    # asyncio guest started running. We use this to restore it at the
    # end of the asyncio run. asyncio always takes over the wakeup fd
    # and resets it to -1 when complete, so if the host loop also uses
    # set_wakeup_fd(), it must (like Trio) only use it for wakeups and
    # not for signal dispatching. Due to this requirement, when restoring
    # the fd we use warn_on_full_buffer=False. (It's not possible to
    # determine what warn_on_full_buffer was originally set to.)
    prev_wakeup_fd: Optional[int]

    # Callbacks that were passed to start_guest_run()
    run_sync_soon_threadsafe: Callable[[Callable[[], None]], None]
    run_sync_soon_not_threadsafe: Callable[[Callable[[], None]], None]
    done_callback: Callable[[Outcome], None]

    # The greenlet wrapping our asyncio run. It has attributes:
    #   unpatch: callable that undoes our monkeypatching so it can continue
    #            without host-loop cooperation
    #   thread_outcome: outcome of the most recent deferred-to-thread call,
    #                   used in recovery from host loop abandonment
    run_greenlet: greenlet.greenlet

    # The next thing we plan to send to the greenlet
    run_next_send: Any

    def __attrs_post_init__(self):
        undo_list = []
        self.run_greenlet.unpatch = partial(unpatch, undo_list)
        self.loop._aioguest_guest_tick_scheduled = False
        if not isinstance(self.loop, asyncio.BaseEventLoop):
            raise RuntimeError(
                f"Only instances of asyncio.BaseEventLoop are supported, not "
                f"{type(self.loop).__name__!r}"
            )
        patch_one(self.loop, EventLoopOverrides, undo_list)
        selector = self.loop._selector
        if isinstance(self.loop, getattr(asyncio, "ProactorEventLoop", ())):
            if type(selector) is not getattr(asyncio, "IocpProactor", None):
                raise RuntimeError(
                    "Only the IocpProactor is supported when using a proactor-based "
                    f"event loop, not {type(selector).__name__}"
                )
            patch_one(selector, ProactorOverrides, undo_list)
        else:
            if type(selector) not in (
                getattr(selectors, "EpollSelector", None),
                getattr(selectors, "DevpollSelector", None),
                getattr(selectors, "KqueueSelector", None),
            ):
                raise RuntimeError(
                    "Only thread-safe selectors (epoll, devpoll, or kqueue) are "
                    "supported when using a selector-based event loop, not "
                    f"{type(selector).__name__}"
                )
            patch_one(selector, SelectorOverrides, undo_list)

    def __del__(self):
        if not self.run_greenlet:
            return

        if sys.implementation.name == "pypy":
            # PyPy does not throw an exception into its dead greenlets; they
            # and the contents of their frames just get destroyed by GC.
            # Mimic the CPython behavior so we have a chance to notice
            # abandonment by the event loop.
            try:
                result = self.run_greenlet.throw(greenlet.GreenletExit)
            except greenlet.error:  # pragma: no cover
                # Probably we're running on the wrong thread.
                # PyPy doesn't have AddPendingCall. We could try to create
                # another reference to the greenlet in order to defer its
                # collection, but I don't understand the PyPy GC well enough
                # to know if that would work.
                warnings.warn(
                    RuntimeWarning(
                        "asyncio guest run got abandoned without properly finishing, "
                        "and it was GC'ed on the wrong thread so we couldn't unwind "
                        "it. Weird stuff will probably happen."
                    )
                )
            else:
                report_abandoned(result)
            return

        # CPython
        gr = weakref.ref(self.run_greenlet)
        del self.run_greenlet
        if gr() is not None:
            # Greenlet was resurrected because it belongs to a
            # different thread than the thread where __del__ is
            # running; it needs to be finalized on its own thread
            # (because you can't run a greenlet on a thread other than
            # the one it was created on, because it owns part of the C
            # stack of that thread). This will happen as soon as that
            # thread calls greenlet.getcurrent()... which is difficult
            # to force to happen, and is not particularly likely to
            # happen, because the host event loop abandoned us and
            # nothing else in the application is necessarily going to
            # use greenlets. We can try a few things though.
            CheckForZombieGreenlets.soon(gr)

    def guest_tick(self):
        prev_loop_name = sniffio_loop.name
        sniffio_loop.name = "asyncio"
        request = self.run_greenlet.switch(self.run_next_send)
        sniffio_loop.name = prev_loop_name

        if isinstance(request, Outcome):
            # Top-level return value from the greenlet, which was running
            # outcome.capture(something)
            self.run_greenlet.unpatch()
            if self.prev_wakeup_fd is not None:
                signal.set_wakeup_fd(self.prev_wakeup_fd, warn_on_full_buffer=False)
            self.done_callback(request)
            return

        if request is None:
            # I/O was synchronously available, or no I/O wait timeout.
            # We don't need to go into the thread.
            self.run_next_send = None
            self.loop._aioguest_guest_tick_scheduled = True
            self.run_sync_soon_not_threadsafe(self.guest_tick)
            return

        # I/O wait (supplied as a callable) needs to be performed in a
        # worker thread, and the result sent back in once it's available.
        assert callable(request)
        self.loop._aioguest_guest_tick_scheduled = False

        def deliver(outcome):
            nonlocal self  # so we can 'del' it below

            def in_main_thread():
                self.run_next_send = outcome
                del self.run_greenlet.thread_outcome
                self.loop._aioguest_guest_tick_scheduled = True
                self.guest_tick()

            self.run_greenlet.thread_outcome = outcome
            try:
                self.run_sync_soon_threadsafe(in_main_thread)
            except BaseException:
                # If the host loop stopped running and abandoned us,
                # that might manifest in run_sync_soon_threadsafe
                # raising an exception. Print a warning more useful
                # than "unhandled exception in thread".
                log.exception(
                    "Exception in aioguest calling run_sync_soon_threadsafe; "
                    "likely the host loop has abandoned us"
                )
                # This helps break a cycle so the issue will be noticed sooner
                del self

        start_thread_soon(request, deliver)


def manage_guest_run(runner, coro):
    # On Pythons 3.11 and later, with asyncio.Runner, we can get the loop
    # before calling run(), so we just need a thunk that does run + close.
    __tracebackhide__ = True

    def determine_main_task():
        for task in asyncio.all_tasks():
            if task.get_coro() is coro:
                greenlet.getcurrent().main_task = task
                break
        else:
            greenlet.getcurrent().main_task = None

    # Runner.run() checks this too, but if it throws, it will be inside the
    # 'with runner:' block, and during unwinding runner.close() will try to
    # do loop.run_until_complete(loop.shutdown_asyncgens()), which creates
    # a confusing 2nd error plus an unawaited coroutine warning.
    if asyncio._get_running_loop() is not None:
        coro.close()
        runner.get_loop().close()
        raise RuntimeError(
            "start_guest_run() cannot be called when an asyncio event loop is "
            "already running"
        )

    with runner:
        runner.get_loop().call_soon(determine_main_task)
        return runner.run(coro)


async def wrap_main(coro):
    # On Pythons before 3.11, we don't have asyncio.Runner, so we can't
    # find the event loop created by asyncio.run() until it's already
    # running. This wraps the main task to send the loop to start_guest_run
    # so it can add its monkeypatching before anything interesting happens.
    __tracebackhide__ = True
    try:
        greenlet.getcurrent().main_task = asyncio.current_task()
        parent = greenlet.getcurrent().parent
        # If there is a problem doing the monkeypatching, it will be
        # raised here:
        parent.switch(asyncio.get_running_loop())
    except BaseException:
        coro.close()
        raise
    return await coro


def start_guest_run(
    coro,
    *,
    run_sync_soon_threadsafe,
    done_callback,
    run_sync_soon_not_threadsafe=None,
    debug=None,
    loop_factory=None,
):
    if run_sync_soon_not_threadsafe is None:
        run_sync_soon_not_threadsafe = run_sync_soon_threadsafe

    if threading.current_thread() is threading.main_thread():
        prev_wakeup_fd = signal.set_wakeup_fd(-1)
        signal.set_wakeup_fd(prev_wakeup_fd, warn_on_full_buffer=False)
    else:
        prev_wakeup_fd = None

    if hasattr(asyncio, "Runner"):
        try:
            runner = asyncio.Runner(debug=debug, loop_factory=loop_factory)
            loop = runner.get_loop()
            loop._aioguest_prev_asyncgen_hooks = sys.get_asyncgen_hooks()
            guest_state = GuestState(
                loop=loop,
                prev_wakeup_fd=prev_wakeup_fd,
                run_sync_soon_threadsafe=run_sync_soon_threadsafe,
                run_sync_soon_not_threadsafe=run_sync_soon_not_threadsafe,
                done_callback=done_callback,
                run_greenlet=greenlet.greenlet(capture),
                run_next_send=partial(manage_guest_run, runner, coro),
            )
        except BaseException:
            coro.close()
            loop.close()
            raise
        # Two ticks to determine main_task: first one goes from loop init to
        # first I/O check, second one spawns the task we enqueued
        req = []
        guest_state.run_sync_soon_not_threadsafe = req.append
        guest_state.guest_tick()
        guest_state.run_sync_soon_not_threadsafe = run_sync_soon_not_threadsafe
        if req:
            req.pop()()
            assert not req
    else:
        wrapped_coro = wrap_main(coro)
        try:
            if loop_factory is not None:
                raise TypeError("loop_factory is only supported on Python 3.11+")
            prev_asyncgen_hooks = sys.get_asyncgen_hooks()
            glet = greenlet.greenlet(capture)
        except BaseException:
            coro.close()
            wrapped_coro.close()
            raise
        loop = glet.switch(partial(asyncio.run, wrapped_coro, debug=debug))
        if isinstance(loop, Outcome):
            # asyncio.run() finished before first switch, probably due to error
            done_callback(loop)
            coro.close()
            wrapped_coro.close()
            return None
        loop._aioguest_prev_asyncgen_hooks = prev_asyncgen_hooks
        try:
            guest_state = GuestState(
                loop=loop,
                prev_wakeup_fd=prev_wakeup_fd,
                run_sync_soon_threadsafe=run_sync_soon_threadsafe,
                run_sync_soon_not_threadsafe=run_sync_soon_not_threadsafe,
                done_callback=done_callback,
                run_greenlet=glet,
                run_next_send=None,
            )
        except BaseException as exc:
            # Make sure any exception propagates out of asyncio.run() so that
            # the loop/etc can be torn down
            glet.unpatch()
            result = glet.throw(exc)
            if isinstance(result, Error):
                result.unwrap()
            raise

        # Need to redo this initialization to get our patched versions
        sys.set_asyncgen_hooks(
            firstiter=loop._asyncgen_firstiter_hook,
            finalizer=loop._asyncgen_finalizer_hook,
        )
        guest_state.guest_tick()

    return None if guest_state.run_greenlet.dead else guest_state.run_greenlet.main_task
