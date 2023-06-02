aioguest: run asyncio and another event loop in the same thread
===============================================================

.. image:: https://img.shields.io/pypi/v/aioguest.svg
   :target: https://pypi.org/project/aioguest
   :alt: Latest PyPI version

.. image:: https://github.com/oremanj/aioguest/actions/workflows/ci.yml/badge.svg
   :target: https://github.com/oremanj/aioguest/actions/workflows/ci.yml
   :alt: Automated test status

.. image:: https://codecov.io/gh/oremanj/aioguest/branch/master/graph/badge.svg
   :target: https://codecov.io/gh/oremanj/aioguest
   :alt: Test coverage

.. image:: https://img.shields.io/badge/code%20style-black-000000.svg
   :target: https://github.com/ambv/black
   :alt: Code style: black

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
also supply a *loop_factory* which will be passed to ``asyncio.Runner()``.

The parameters *run_sync_soon_threadsafe*, *done_callback*, and (optionally)
*run_sync_soon_not_threadsafe* tell ``aioguest`` how to interact with the host
event loop. Someday ``aioguest`` will have documentation of its own.
Until then, see the `Trio documentation
<https://trio.readthedocs.io/en/stable/reference-lowlevel.html#reference>`__
for details on these parameters, including an `example
<https://trio.readthedocs.io/en/stable/reference-lowlevel.html#implementing-guest-mode-for-your-favorite-event-loop>`__.

``start_guest_run()`` returns the main task of the new asyncio run, i.e.,
the ``asyncio.Task`` that wraps *coro*. It may also return None if the main task
could not be determined, such as because ``asyncio.run()`` raised an exception
before starting to execute *coro*. The main task is provided mostly for
cancellation purposes; while you can also register callbacks upon its completion,
the run is not necessarily finished at that point, because background tasks and
async generators might still be in the process of finalization.

Exceptions noticed when starting up the asyncio run might either propagate out of
``start_guest_run()`` or be delivered to your *done_callback*, maybe even before
``start_guest_run()`` returns (it will return None in that case). In general,
problems noticed by ``aioguest`` will propagate out of ``start_guest_run()``,
while problems noticed by asyncio will be delivered to your *done_callback*.

``aioguest`` requires Python 3.8 or later. Out of the box it supports
the default asyncio event loop implementation (only) on Linux,
Windows, macOS, FreeBSD, and maybe others. It does not support
operating systems that provide only ``select()`` or ``poll()``; due to
thread-safety considerations it needs an I/O abstraction where the OS
kernel is involved in registrations, such as IOCP, epoll, or kqueue.
Alternative Python-based event loops can likely be supported given
modest effort if they use such an abstraction. Alternative C-based
event loops (such as uvloop) present much more of a challenge because
compiled code generally can't be monkeypatched.

Development status
------------------

``aioguest`` has been tested with a variety of toy examples and
pathological cases, and its unit tests exercise full coverage. It
hasn't had a lot of exposure to real-world problems yet.  Maybe you'd
like to expose it to yours?

License
-------

``aioguest`` is licensed under your choice of the MIT or Apache 2.0
license. See `LICENSE <https://github.com/oremanj/aioguest/blob/master/LICENSE>`__
for details.
