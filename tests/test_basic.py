# This file is closely modeled on trio/_core/tests/test_guest_mode.py
# with trio and asyncio swapped.

import aioguest
import asyncio
import gc
import outcome
import pytest
import queue
import signal
import sniffio
import socket
import sys
import time
import threading
import trio
import types
from contextlib import contextmanager
from functools import partial


def gc_collect_harder():
    # In the test suite we sometimes want to call gc.collect() to make sure
    # that any objects with noisy __del__ methods (e.g. unawaited coroutines)
    # get collected before we continue, so their noise doesn't leak into
    # unrelated tests.
    #
    # On PyPy, coroutine objects (for example) can survive at least 1 round of
    # garbage collection, because executing their __del__ method to print the
    # warning can cause them to be resurrected. So we call collect a few times
    # to make sure.
    for _ in range(5):
        gc.collect()


@contextmanager
def restore_unraisablehook():
    sys.unraisablehook, prev = sys.__unraisablehook__, sys.unraisablehook
    try:
        yield
    finally:
        sys.unraisablehook = prev


# The simplest possible "host" loop.
# Nice features:
# - we can run code outside of asyncio using the schedule function passed to
#   our main
# - final result is returned
# - any unhandled exceptions cause an immediate crash
def trivial_guest_run(aio_fn, **start_guest_run_kwargs):
    todo = queue.Queue()

    host_thread = threading.current_thread()

    def run_sync_soon_threadsafe(fn):
        if host_thread is threading.current_thread():  # pragma: no cover
            crash = partial(
                pytest.fail, "run_sync_soon_threadsafe called from host thread"
            )
            todo.put(("run", crash))
        todo.put(("run", fn))

    def run_sync_soon_not_threadsafe(fn):
        if host_thread is not threading.current_thread():  # pragma: no cover
            crash = partial(
                pytest.fail, "run_sync_soon_not_threadsafe called from worker thread"
            )
            todo.put(("run", crash))
        todo.put(("run", fn))

    def done_callback(outcome):
        todo.put(("unwrap", outcome))

    aioguest.start_guest_run(
        aio_fn(run_sync_soon_not_threadsafe),
        run_sync_soon_threadsafe=run_sync_soon_threadsafe,
        run_sync_soon_not_threadsafe=run_sync_soon_not_threadsafe,
        done_callback=done_callback,
        **start_guest_run_kwargs,
    )

    try:
        while True:
            op, obj = todo.get()
            if op == "run":
                obj()  # pragma: no cover  # ??? definitely does run
            elif op == "unwrap":
                return obj.unwrap()
            else:  # pragma: no cover
                assert False
    finally:
        # Make sure that exceptions raised here don't capture these, so that
        # if an exception does cause us to abandon a run then the guest state
        # has a chance to be GC'ed and warn about it.
        del todo, run_sync_soon_threadsafe, done_callback


def test_guest_trivial():
    async def aio_return(in_host):
        await asyncio.sleep(0)
        return "ok"

    assert trivial_guest_run(aio_return) == "ok"

    async def run_it_in_thread():
        return await asyncio.get_running_loop().run_in_executor(
            None, trivial_guest_run, aio_return
        )

    assert asyncio.run(run_it_in_thread()) == "ok"

    async def aio_fail(in_host):
        raise KeyError("whoopsiedaisy")

    with pytest.raises(KeyError, match="whoopsiedaisy"):
        trivial_guest_run(aio_fail)


def test_guest_can_do_io():
    async def aio_main(in_host):
        record = []
        a, b = socket.socketpair()
        a_r, a_w = await asyncio.open_connection(sock=a)
        b_r, b_w = await asyncio.open_connection(sock=b)
        try:
            receiver_is_running = asyncio.Event()

            async def do_receive():
                receiver_is_running.set()
                record.append(await a_r.read(1))

            receive_task = asyncio.create_task(do_receive())
            await receiver_is_running.wait()
            b_w.write(b"x")
            await b_w.drain()
            await receive_task
        finally:
            a_w.close()
            b_w.close()

        assert record == [b"x"]

    trivial_guest_run(aio_main)


def test_host_can_directly_wake_aio_task():
    async def aio_main(in_host):
        ev = asyncio.Event()
        in_host(ev.set)
        await ev.wait()
        return "ok"

    assert trivial_guest_run(aio_main) == "ok"


def test_host_timer_sched_wakes_aio_up():
    async def aio_main(in_host):
        this_task = asyncio.current_task()
        loop = asyncio.get_running_loop()

        # First scheduled callback
        in_host(lambda: loop.call_later(0.2, this_task.cancel))
        # Scheduled callback that is not the new earliest
        in_host(lambda: loop.call_later(0.5, this_task.cancel))
        # Scheduled callback that is the new earliest
        in_host(lambda: loop.call_later(0.1, this_task.cancel))

        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(10)

        return "ok"

    assert trivial_guest_run(aio_main) == "ok"


def test_wakeup_fd_is_restored():
    assert signal.set_wakeup_fd(-1) == -1

    async def aio_main(in_host, *, try_signal=False):
        # If we can use signals, verify that the wakeup fd actually wakes us.
        # Note that on Unix asyncio generally doesn't set a wakeup fd until
        # the first signal handler is registered.
        if try_signal:
            loop = asyncio.get_running_loop()
            evt = asyncio.Event()
            loop.add_signal_handler(signal.SIGALRM, evt.set)
        ret = signal.set_wakeup_fd(-1)
        signal.set_wakeup_fd(ret)
        if try_signal:
            signal.setitimer(signal.ITIMER_REAL, 0.1)
            await evt.wait()
        return ret

    trivial_guest_run(aio_main)
    main_with_fd = partial(aio_main, try_signal=(sys.platform != "win32"))
    assert trivial_guest_run(main_with_fd) != -1

    a, b = socket.socketpair()
    with a, b:
        a.setblocking(False)

        # Verify that wakeup fd was restored to -1 at end of previous
        # guest run, and that it gets restored to a.fileno() at the end
        # of the upcoming one
        assert signal.set_wakeup_fd(a.fileno()) == -1
        trivial_guest_run(aio_main)
        assert signal.set_wakeup_fd(-1) == a.fileno()

        # Another test, this time ensuring that aio actually changes the fd
        assert signal.set_wakeup_fd(a.fileno()) == -1
        assert -1 != trivial_guest_run(main_with_fd) != a.fileno()
        assert signal.set_wakeup_fd(-1) == a.fileno()


@pytest.mark.parametrize("slow_cancel", (False, True))
@pytest.mark.parametrize("create_task", (False, True))
@pytest.mark.parametrize("main_exits", (False, True))
@restore_unraisablehook()
def test_guest_warns_if_abandoned(main_exits, create_task, slow_cancel):
    # This warning is emitted from the garbage collector. So we have to make
    # sure that our abandoned run is garbage. The easiest way to do this is to
    # put it into a function, so that we're sure all the local state,
    # traceback frames, etc. are garbage once it returns.
    record = []

    def do_abandoned_guest_run():
        async def other_task():
            try:
                print("other_task sleeping @", asyncio.get_running_loop().time())
                await asyncio.sleep(0.5)
                print("other_task waking @", asyncio.get_running_loop().time())
                record.append("other completed")
            except asyncio.CancelledError as exc1:
                print("other_task cancelled @", asyncio.get_running_loop().time())
                if slow_cancel:
                    try:
                        await asyncio.sleep(1)
                    except asyncio.CancelledError as exc2:
                        # One cancel is done by asyncio.Runner when the main
                        # task exits. The other is done by aioguest if the loop is
                        # abandoned after the main task exits
                        assert "aioguest" in str(exc1) + str(exc2)
                print("other_task returning @", asyncio.get_running_loop().time())
                record.append("other cancelled")

        async def abandoned_main(in_host):
            if create_task:
                asyncio.create_task(other_task())
                # Make sure other_task actually starts; asyncio tasks can be
                # cancelled before their first tick.
                await asyncio.sleep(0)
            print("main_task crashing @", asyncio.get_running_loop().time())
            in_host(lambda: 1 / 0)
            try:
                if not main_exits:
                    for _ in range(50):  # pragma: no branch  # loop always interrupted
                        await asyncio.sleep(0.05)
                print("main_task finishing @", asyncio.get_running_loop().time())
                record.append("completed")
            except asyncio.CancelledError as exc:
                assert "aioguest" in str(exc)
                print("main_task cancelled @", asyncio.get_running_loop().time())
                if slow_cancel:
                    await asyncio.sleep(1)
                print("main_task returning @", asyncio.get_running_loop().time())
                record.append("cancelled")

        with pytest.raises(ZeroDivisionError):
            trivial_guest_run(abandoned_main)

    with pytest.warns(RuntimeWarning, match="asyncio guest run got abandoned"):
        do_abandoned_guest_run()
        gc_collect_harder()

        # If we abandoned the run with I/O in flight that had a
        # nonzero timeout, then that I/O might still be running in a
        # background thread.  We won't actually notice the abandonment
        # until it completes, because in the meantime that thread's
        # deliver callback holds a reference to the guest state.
        deadline = time.monotonic() + 2.0
        while record == [] and time.monotonic() < deadline:  # pragma: no cover
            time.sleep(0.1)
            gc_collect_harder()
        if record == []:  # pragma: no cover
            gc_collect_harder()

        # If you have problems some day figuring out what's holding onto a
        # reference to the guest state and making this test fail,
        # then this might be useful to help track it down. (It assumes you
        # also hack start_guest_run so that it does 'global W; W =
        # weakref(guest_state)'.) Note that if the GuestState is being
        # destroyed but the greenlet isn't, this won't help as much; while
        # greenlets are weak-referenceable, GC can't see cycles that involve
        # both a greenlet and its frames.
        # (see https://greenlet.readthedocs.io/en/latest/greenlet_gc.html)
        #
        # import gc
        # print(aioguest._impl.W)
        # targets = [aioguest._impl.W()]
        # for i in range(15):
        #     new_targets = []
        #     for target in targets:
        #         new_targets += gc.get_referrers(target)
        #         new_targets.remove(targets)
        #     print("#####################")
        #     print(f"depth {i}: {len(new_targets)}")
        #     print(new_targets)
        #     targets = new_targets

    if not create_task:
        assert record == ["completed" if main_exits else "cancelled"]
    elif main_exits:
        # Main task was not running at abandonment so all tasks were cancelled
        assert record == ["completed", "other cancelled"]
    else:
        # Main task was running at abandonment so only it was cancelled;
        # when it exited the runner cancelled the others. With slow_cancel
        # the other_task completes before main_task finishes cancelling.
        if slow_cancel:
            assert record == ["other completed", "cancelled"]
        else:
            assert record == ["cancelled", "other cancelled"]

    # Make sure the loop was in fact torn down
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()


@pytest.mark.parametrize("slow_cancel", (False, True))
@pytest.mark.parametrize("create_task", (False, True))
@pytest.mark.parametrize("main_exits", (False, True))
@restore_unraisablehook()
def test_guest_in_other_thread_warns_if_abandoned(main_exits, create_task, slow_cancel):
    result = outcome.Error(trio.TooSlowError())

    def do_it():
        nonlocal result
        result = outcome.capture(
            test_guest_warns_if_abandoned, main_exits, create_task, slow_cancel
        )

    t = threading.Thread(target=do_it)
    t.start()
    t.join(3)
    result.unwrap()


@pytest.mark.parametrize(
    "result_factory",
    (partial(outcome.Value, 42), lambda: outcome.Error(RuntimeError("hi"))),
)
@restore_unraisablehook()
def test_abandoned_guest_logs_result(result_factory, caplog):
    async def abandoned_main(in_host):
        in_host(lambda: 1 / 0)
        with pytest.raises(asyncio.CancelledError, match="aioguest"):
            for _ in range(50):  # pragma: no branch
                await asyncio.sleep(0)
        del in_host
        return result_factory().unwrap()

    with pytest.warns(RuntimeWarning, match="asyncio guest run got abandoned"):
        with pytest.raises(ZeroDivisionError):
            trivial_guest_run(abandoned_main)
        gc_collect_harder()

    if isinstance(result_factory(), outcome.Value):
        assert "couldn't deliver the guest result: 42" in caplog.text
    else:
        assert "couldn't deliver the guest exception:" in caplog.text


def aio_in_trio_run(aio_fn, *, pass_not_threadsafe=True, **start_guest_run_kwargs):
    async def trio_main():
        waiting_trio_task = None
        aio_outcome = None

        def aio_done_callback(main_outcome):
            nonlocal aio_outcome
            aio_outcome = main_outcome
            print(f"aio_fn finished: {main_outcome!r}")
            if waiting_trio_task is not None:
                trio.lowlevel.reschedule(waiting_trio_task, aio_outcome)

        trio_token = trio.lowlevel.current_trio_token()
        if pass_not_threadsafe:
            start_guest_run_kwargs["run_sync_soon_not_threadsafe"] = (
                trio_token.run_sync_soon
            )

        async with trio.open_nursery() as nursery:
            aio_main_task = aioguest.start_guest_run(
                aio_fn(nursery),
                run_sync_soon_threadsafe=trio_token.run_sync_soon,
                done_callback=aio_done_callback,
                **start_guest_run_kwargs,
            )
            outer_raise_cancel = None

            def abort_fn(raise_cancel):
                nonlocal outer_raise_cancel
                outer_raise_cancel = raise_cancel
                if aio_main_task is not None and not aio_main_task.done():
                    aio_main_task.cancel()
                else:  # pragma: no cover
                    for task in asyncio.all_tasks():
                        task.cancel()
                return trio.lowlevel.Abort.FAILED

            try:
                if aio_outcome is not None:
                    return aio_outcome.unwrap()
                waiting_trio_task = trio.lowlevel.current_task()
                return await trio.lowlevel.wait_task_rescheduled(abort_fn)
            except asyncio.CancelledError:
                if outer_raise_cancel is not None:
                    outer_raise_cancel()
                raise

    return trio.run(trio_main)


def test_guest_mode_on_trio():
    async def aio_main(nursery):
        print("aio_main!")

        to_trio, from_aio = trio.open_memory_channel(float("inf"))
        from_trio = asyncio.Queue()

        pingpong_cscope = trio.CancelScope()
        nursery.start_soon(trio_pingpong, pingpong_cscope, from_aio, from_trio)

        # Make sure we have at least one tick where we don't need to go into
        # the thread
        await asyncio.sleep(0)

        to_trio.send_nowait(0)

        while True:
            n = await from_trio.get()
            print(f"aio got: {n}")
            to_trio.send_nowait(n + 1)
            if n >= 10:
                pingpong_cscope.cancel()
                return "aio-main-done"

    async def trio_pingpong(cscope, from_aio, to_aio):
        print("trio_pingpong!")

        with cscope:
            while True:
                n = await from_aio.receive()
                print(f"trio got: {n}")
                to_aio.put_nowait(n + 1)
        assert cscope.cancelled_caught

    assert aio_in_trio_run(aio_main) == "aio-main-done"

    # Also check that passing only call_soon_threadsafe works, via the
    # fallback path where we use it for everything.
    assert aio_in_trio_run(aio_main, pass_not_threadsafe=False) == "aio-main-done"


def test_aio_in_trio_cancellation():
    async def aio_main(nursery):
        @nursery.start_soon
        async def raise_it():
            raise RuntimeError("whoops")

        await asyncio.sleep(5)

    with pytest.raises(RuntimeError, match="whoops"):
        aio_in_trio_run(aio_main)

    async def aio_main_cancel_internally(nursery):
        asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
        await asyncio.sleep(5)

    with pytest.raises(asyncio.CancelledError):
        aio_in_trio_run(aio_main_cancel_internally)


def test_guest_mode_internal_errors(monkeypatch, recwarn):
    with monkeypatch.context() as m:

        async def crash_in_run_loop(in_host):
            loop = asyncio.get_running_loop()
            m.setattr(loop, "_ready", "hi")
            await asyncio.sleep(1)

        with pytest.raises(AttributeError):
            trivial_guest_run(crash_in_run_loop)

    with monkeypatch.context() as m:

        async def crash_in_io(in_host):
            loop = asyncio.get_running_loop()
            m.setattr(loop._selector, "select", None)
            await asyncio.sleep(0)

        with pytest.raises(TypeError):
            trivial_guest_run(crash_in_io)

    with monkeypatch.context() as m:

        async def crash_in_worker_thread_io(in_host):
            t = threading.current_thread()
            if sys.platform == "win32":
                import _overlapped

                loc = (_overlapped, "GetQueuedCompletionStatus")
            else:
                import selectors

                loc = (selectors.DefaultSelector, "select")

            old_get_events = getattr(*loc)

            def bad_get_events(*args):
                if threading.current_thread() is not t:
                    raise ValueError("oh no!")
                else:
                    return old_get_events(*args)

            m.setattr(*loc, bad_get_events)
            await asyncio.sleep(1)

        with pytest.raises(ValueError):
            trivial_guest_run(crash_in_worker_thread_io)

    gc_collect_harder()


@restore_unraisablehook()
def test_guest_mode_asyncgens():
    import sniffio

    record = set()

    async def agen(label):
        assert sniffio.current_async_library() == label
        try:
            yield 1
        finally:
            library = sniffio.current_async_library()
            try:
                await sys.modules[library].sleep(0)
            except (trio.Cancelled, asyncio.CancelledError):
                pass
            record.add((label, library))

    async def iterate_in_trio(done_evt):
        await agen("trio").asend(None)
        done_evt.set()

    async def aio_main(nursery):
        done_evt = asyncio.Event()
        # TODO: switch trio to the sniffio thread-local so we don't need
        # this hoop-jumping; otherwise iterate_in_trio inherits the asyncio
        # context, which is empty
        # Should be: nursery.start_soon(iterate_in_trio, done_evt)
        nursery.parent_task.context.run(
            nursery.start_soon, iterate_in_trio, done_evt
        )
        await asyncio.wait_for(done_evt.wait(), 1)

        await agen("asyncio").asend(None)
        gc_collect_harder()

        # The asyncio async generator finalizer enqueues a create_task
        # call, but asyncio doesn't ensure that all callbacks enqueued
        # before the main task exits run before the loop shuts down.
        # This yield is necessary to make the enqueued callback run.
        await asyncio.sleep(0)

    aio_in_trio_run(aio_main)
    assert record == {("trio", "trio"), ("asyncio", "asyncio")}


@restore_unraisablehook()
def test_host_asyncgen_without_host_hooks(capsys):
    record = []

    @types.coroutine
    def yield_briefly():
        yield

    async def agen(try_to_yield):
        with pytest.raises(sniffio.AsyncLibraryNotFoundError):
            sniffio.current_async_library()
        try:
            yield 1
        finally:
            with pytest.raises(sniffio.AsyncLibraryNotFoundError):
                sniffio.current_async_library()
            record.append(try_to_yield)
            if try_to_yield:
                await yield_briefly()

    def iter_and_discard(arg, evt):
        with pytest.raises(StopIteration, match="1"):
            agen(arg).asend(None).send(None)
        evt.set()

    async def aio_main(in_host):
        in_host(partial(iter_and_discard, False, evt := asyncio.Event()))
        await asyncio.wait_for(evt.wait(), 1)
        gc_collect_harder()
        assert record.pop() == False
        assert capsys.readouterr()[1] == ""

        in_host(partial(iter_and_discard, True, evt := asyncio.Event()))
        await asyncio.wait_for(evt.wait(), 1)
        gc_collect_harder()
        assert record.pop() == True
        assert "awaited something during finalization" in capsys.readouterr()[1]

    trivial_guest_run(aio_main)


def test_errors(monkeypatch):
    async def noop(in_host):
        pass  # pragma: no cover

    async def already_in_aio(*args):
        with pytest.raises(
            RuntimeError, match="asyncio.run.. cannot be called .* running event loop"
        ):
            trivial_guest_run(already_in_aio)
        with pytest.raises(
            RuntimeError, match="asyncio.run.. cannot be called .* running event loop"
        ):
            aio_in_trio_run(already_in_aio)

    asyncio.run(already_in_aio())

    with monkeypatch.context() as m:
        m.setattr(asyncio, "BaseEventLoop", int)
        with pytest.raises(
            RuntimeError, match="Only instances of asyncio.BaseEventLoop are supported"
        ):
            trivial_guest_run(noop)

    with monkeypatch.context() as m:
        import selectors

        m.setattr(
            asyncio.get_event_loop_policy(),
            "new_event_loop",
            lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )

        with pytest.raises(
            RuntimeError, match="Only thread-safe selectors .* are supported"
        ):
            trivial_guest_run(noop)

    if sys.platform == "win32":
        with monkeypatch.context() as m:
            m.setattr(asyncio, "IocpProactor", int)
            with pytest.raises(RuntimeError, match="Only the IocpProactor is supported"):
                trivial_guest_run(noop)

    if sys.version_info < (3, 11):
        with pytest.raises(TypeError, match="only supported on Python 3.11+"):
            trivial_guest_run(noop, loop_factory=lambda: None)

    gc_collect_harder()
