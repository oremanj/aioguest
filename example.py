import aioguest, asyncio, trio

# This is the example from Trio's guest mode docs, with the loops swapped.


# A tiny asyncio program
async def aio_main():
    for _ in range(5):
        print("Hello from asyncio!")
        await asyncio.sleep(1)
    return "trio done!"


# The code to run it as a guest inside Trio
async def trio_main():
    trio_token = trio.lowlevel.current_trio_token()
    done_evt = trio.Event()
    aio_outcome = None

    def done_callback(outcome):
        nonlocal aio_outcome
        aio_outcome = outcome
        done_evt.set()

    aioguest.start_guest_run(
        aio_main(),
        run_sync_soon_threadsafe=trio_token.run_sync_soon,
        done_callback=done_callback,
    )

    # Wait for the guest run to finish
    await done_evt.wait()

    # Pass through the return value or exception from the guest run
    return aio_outcome.unwrap()


trio.run(trio_main)
