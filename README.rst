aioguest: run asyncio and another event loop in the same thread
===============================================================

`Trio <https://github.com/python-trio/trio>`__, an alternate async framework
for Python, supports a feature called `"guest mode"
<https://trio.readthedocs.io/en/stable/reference-lowlevel.html#using-guest-mode-to-run-trio-on-top-of-other-event-loops>`__ where it can run in the same thread as
another event loop. In guest mode, low-level I/O waiting occurs on a worker thread,
but the threading is invisible to user code, and both event loops can interact
with each other without any special synchronization.

This package implements guest mode for asyncio. It has one public function::

    aioguest.start_guest_run(
        coro,
        *,
        run_sync_soon_threadsafe,
        done_callback,
        run_sync_soon_not_threadsafe=None,
        debug=None,
        loop_factory=None,
    )

This effectively starts a call to ``asyncio.run(coro)`` in parallel
with the currently running ("host") event loop. The *debug* parameter
is passed to ``asyncio.run()`` if specified.  On Python 3.11+, you can
supply a *loop_factory* which will be passed to ``asyncio.Runner()``.

The parameters *run_sync_soon_threadsafe*, *done_callback*, and (optionally)
*run_sync_soon_not_threadsafe* tell ``aioguest`` how to interact with the host
event loop. Someday ``aioguest`` will have documentation of its own.
Until then, see the `Trio documentation
<https://trio.readthedocs.io/en/stable/reference-lowlevel.html#reference>`__
for details on these parameters, including an `example
<https://trio.readthedocs.io/en/stable/reference-lowlevel.html#implementing-guest-mode-for-your-favorite-event-loop>`__.

Development status (caution!)
-----------------------------

Pre-alpha. Right now this is something I hacked together in an
evening, without any meaningful test coverage. I'm sure it has bugs.
I hope to improve the situation shortly.

License
-------

``aioguest`` is licensed under your choice of the MIT or Apache 2.0
license. See `LICENSE <https://github.com/oremanj/aioguest/blob/master/LICENSE>`__
for details.
