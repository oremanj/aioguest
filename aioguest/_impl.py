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
import selectors
import signal
import sys
import threading
from trio.lowlevel import start_thread_soon
from sniffio import thread_local as sniffio_loop
from functools import partial
from outcome import Outcome, Value, Error, capture
from typing import Optional, Callable, Any


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
    gr = greenlet.getcurrent()
    try:
        return gr.parent.switch(request)
    except greenlet.GreenletExit:
        warnings.warn(
            RuntimeWarning(
                "asyncio guest run got abandoned without properly finishing... "
                "weird stuff might happen"
            )
        )
        gr.unpatch()
        return getattr(gr, "thread_outcome", None)


# Overrides for any selectors.BaseSelector that uses a thread-safe
# (kernel-based) mechanism for tracking interest: kqueue, epoll, or devpoll.
class SelectorOverrides:
    def select(self, timeout=None):
        # Avoid the worker thread if possible: if we weren't asked to wait,
        # or if any I/O was immediately ready, then just do a brief yield.
        immediate_result = super().select(0)
        if immediate_result or timeout <= 0:
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

    def __setattr__(self, name, value):
        raise AttributeError("unexpected attribute assignment")


# Overrides for asyncio.IocpProactor
class ProactorOverrides:
    def select(self, timeout=None):
        # Avoid the worker thread if possible: if we weren't asked to wait,
        # or if any I/O was immediately ready, then just do a brief yield.
        immediate_result = super().select(0)
        if immediate_result or timeout <= 0:
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
        this_function = ProactorOverrides._poll
        this_function.__globals__ = {
            **underlying.__globals__,
            "_overlapped": PatchedOverlappedModule(),
        }
        this_function.__code__ = underlying.__code__
        assert this_function.__defaults__ == underlying.__defaults__
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

    def _add_callback(self, handle):
        was_first = not self._ready
        try:
            return super()._add_callback(handle)
        finally:
            if was_first and not self._aioguest_guest_tick_scheduled:
                self._aioguest_guest_tick_scheduled = True
                self._write_to_self()

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
        for disambiguator in itertools.count():
            name = f"{ty.__name__}{disambiguator or ''}+aioguest"
            if not hasattr(globals(), name):
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
        if isinstance(self.loop, getattr(asyncio, "WindowsEventLoop", ())):
            if type(selector) is not getattr(asyncio, "IocpProactor", None):
                raise RuntimeError(
                    "Only the IocpProactor is supported when using a proactor-based "
                    f"event loop, not {type(selector).__name__}"
                )
            patch_one(selector, ProactorOverides, undo_list)
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
            def in_main_thread():
                self.run_next_send = outcome
                del self.run_greenlet.thread_outcome
                self.loop._aioguest_guest_tick_scheduled = True
                self.guest_tick()

            self.run_greenlet.thread_outcome = outcome
            self.run_sync_soon_threadsafe(in_main_thread)

        start_thread_soon(request, deliver)


def manage_guest_run(runner, coro):
    # On Pythons 3.11 and later, with asyncio.Runner, we can get the loop
    # before calling run(), so we just need a thunk that does run + close.
    __tracebackhide__ = True
    with runner:
        return runner.run(coro)


async def wrap_main(coro):
    # On Pythons before 3.11, we don't have asyncio.Runner, so we can't
    # find the event loop created by asyncio.run() until it's already
    # running. This wraps the main task to send the loop to start_guest_run
    # so it can add its monkeypatching before anything interesting happens.
    __tracebackhide__ = True
    greenlet.getcurrent().parent.switch(asyncio.get_running_loop())
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
    else:
        if loop_factory is not None:
            raise TypeError("loop_factory is only supported on Python 3.11+")

        prev_asyncgen_hooks = sys.get_asyncgen_hooks()
        glet = greenlet.greenlet(capture)
        loop = glet.switch(partial(asyncio.run, wrap_main(coro), debug=debug))
        loop._aioguest_prev_asyncgen_hooks = prev_asyncgen_hooks
        guest_state = GuestState(
            loop=loop,
            prev_wakeup_fd=prev_wakeup_fd,
            run_sync_soon_threadsafe=run_sync_soon_threadsafe,
            run_sync_soon_not_threadsafe=run_sync_soon_not_threadsafe,
            done_callback=done_callback,
            run_greenlet=glet,
            run_next_send=None,
        )
        # Need to redo this initialization to get our patched versions
        sys.set_asyncgen_hooks(
            firstiter=loop._asyncgen_firstiter_hook,
            finalizer=loop._asyncgen_finalizer_hook,
        )
    run_sync_soon_not_threadsafe(guest_state.guest_tick)
