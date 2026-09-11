"""Extensible command dispatch for the Ymir API.

Commands are registered by name via ``register()``. The ``dispatch()``
function tokenises the raw command text with ``shlex.split`` and calls
the matching handler.

Adding a new command:

    from ymir.api import command_parser

    async def handle_my_command(args: list[str], request: web.Request) -> web.Response:
        ...

    command_parser.register("my-command", handle_my_command)
"""

import logging
import shlex
from collections.abc import Awaitable, Callable

from aiohttp import web

logger = logging.getLogger(__name__)

CommandHandler = Callable[[list[str], web.Request], Awaitable[web.Response]]

_REGISTRY: dict[str, CommandHandler] = {}


def register(name: str, handler: CommandHandler) -> None:
    """Register a command handler under *name* (case-insensitive at dispatch)."""
    _REGISTRY[name.lower()] = handler


async def dispatch(command_text: str, request: web.Request) -> web.Response:
    """Tokenise *command_text* and invoke the registered handler.

    Returns a 400 response if the command name is unknown or if
    ``shlex.split`` fails (e.g. unmatched quotes).
    """
    try:
        tokens = shlex.split(command_text)
    except ValueError as exc:
        return web.json_response(
            {"error": f"failed to parse command: {exc}"},
            status=400,
        )

    if not tokens:
        return web.json_response({"error": "empty command"}, status=400)

    command_name = tokens[0].lower()
    handler = _REGISTRY.get(command_name)
    if handler is None:
        return web.json_response(
            {"error": f"unknown command: {command_name}"},
            status=400,
        )

    logger.info("Dispatching command %r with args %s", command_name, tokens[1:])
    return await handler(tokens[1:], request)
