"""Messaging channels (Telegram, WhatsApp) that bridge chats to Cadence agent sessions."""

from .base import Bridge, Channel, build_channels, chunk

__all__ = ["Bridge", "Channel", "build_channels", "chunk"]
