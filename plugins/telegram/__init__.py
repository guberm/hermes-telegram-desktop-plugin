"""Agent-side wrapper for the Telegram Desktop/dashboard plugin package."""

from __future__ import annotations


def register(ctx) -> None:  # noqa: ARG001
    """The Telegram UI and API are owned by the Desktop/dashboard surfaces."""
    return None
