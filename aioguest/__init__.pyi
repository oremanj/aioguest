import asyncio
from outcome import Outcome
from typing import Coroutine, Callable, TypeVar

T = TypeVar("T")

__version__: str

def start_guest_run(
    coro: Coroutine[Any, Any, T],
    *,
    run_sync_soon_threadsafe: Callable[[Callable[[], None]], None],
    done_callback: Callable[[Outcome[T]], None],
    run_sync_soon_not_threadsafe: Callable[[Callable[[], None]], None] | None = None,
    debug: bool | None = None,
    loop_factory: Callable[[], asyncio.AbstractEventLoop] | None = None,
) -> asyncio.Task[T] | None:
    ...
