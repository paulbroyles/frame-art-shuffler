"""Metadata utilities for Frame Art Shuffler."""

from __future__ import annotations

from typing import Optional


class MetadataError(Exception):
    """Base error raised for metadata operations."""


def normalize_mac(mac: Optional[str]) -> Optional[str]:
    """Normalize MAC address to lowercase colon-separated form.

    Returns ``None`` when the input is invalid or empty.
    """

    if not mac or not isinstance(mac, str):
        return None

    cleaned = "".join(ch for ch in mac if ch in "0123456789abcdefABCDEF")
    if len(cleaned) != 12:
        return None
    pairs = [cleaned[i : i + 2] for i in range(0, 12, 2)]
    return ":".join(pair.lower() for pair in pairs)
