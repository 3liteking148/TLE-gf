"""Pure helpers for the channel-gate cog (``channel_gate.py``).

Kept discord-free so the gate decision can be unit-tested without the bot.
"""
from discord.ext import commands


class ChannelGateError(commands.CommandError):
    """User-facing error for the ``;disallow`` / ``;allow`` commands."""
    pass


def gate_decision(gate, current_thread_id):
    """Decide whether a command may run, given the ``command_gate`` row (or
    None) for the invocation's parent channel and the id of the thread it was
    run in (or None if run in the channel itself).

    Returns ``(allowed, allowed_thread_id)``. ``allowed_thread_id`` is the
    channel's designated command thread (may be None) — the cog uses it to link
    the user to where commands still work. IDs are compared as strings because
    the DB stores them as TEXT.
    """
    if gate is None:
        return True, None
    allowed_thread_id = gate.thread_id
    if (current_thread_id is not None and allowed_thread_id is not None
            and str(current_thread_id) == str(allowed_thread_id)):
        return True, allowed_thread_id
    return False, allowed_thread_id
