"""Shared protocol definitions for the backend and the mock bridge."""

from .messages import PROTOCOL_VERSION, ProtocolError

__all__ = ["PROTOCOL_VERSION", "ProtocolError"]
