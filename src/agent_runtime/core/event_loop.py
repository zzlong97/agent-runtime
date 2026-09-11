"""Event loop selection compatible with psycopg async connections."""

import asyncio
import sys


def psycopg_compatible_loop_factory() -> asyncio.AbstractEventLoop:
    """Return a Selector loop on Windows and the platform default elsewhere."""

    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()
