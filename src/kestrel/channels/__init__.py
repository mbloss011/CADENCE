"""Messaging channels (Telegram, WhatsApp) that bridge chats to Kestrel agent sessions."""

from .base import Bridge, Channel, build_channels, chunk

__all__ = ["Bridge", "Channel", "build_channels", "chunk"]
