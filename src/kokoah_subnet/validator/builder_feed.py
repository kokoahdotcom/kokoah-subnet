"""Hyperliquid's per-builder daily fill file: the canonical list of scored fills.

`https://stats-data.hyperliquid.xyz/Mainnet/builder_fills/{builder}/{YYYYMMDD}.csv.lz4` (LZ4 frame). Unknown
builders and days not yet published answer 403 or 404; files can appear late, so a validator retries.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import lz4.frame

from kokoah_subnet.mechanism.fills import Fill, parse_builder_csv

BASE_URL = "https://stats-data.hyperliquid.xyz/Mainnet/builder_fills"
MAX_COMPRESSED = 64 * 1024 * 1024
MAX_TEXT = 512 * 1024 * 1024


class NotPublished(Exception):
    """The file is not (yet) available for this builder and day."""


def file_url(builder: str, day: str, base_url: str = BASE_URL) -> str:
    return f"{base_url.rstrip('/')}/{builder.lower()}/{day.replace('-', '')}.csv.lz4"


def decompress(data: bytes) -> str:
    if len(data) > MAX_COMPRESSED:
        raise ValueError("builder fills file is too large")
    text: bytes = lz4.frame.decompress(data)
    if len(text) > MAX_TEXT:
        raise ValueError("builder fills file decompresses beyond the size limit")
    return text.decode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def download(builder: str, day: str, base_url: str = BASE_URL, timeout_s: float = 60.0) -> bytes:
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
        r = await client.get(file_url(builder, day, base_url))
    if r.status_code in (403, 404):
        raise NotPublished(f"no builder fills file for {builder} on {day} (HTTP {r.status_code})")
    r.raise_for_status()
    return r.content


def load_file(path: Path) -> list[Fill]:
    """Parse a downloaded file (compressed `.lz4` or plain `.csv`)."""
    data = path.read_bytes()
    text = decompress(data) if path.suffix == ".lz4" else data.decode("utf-8")
    return parse_builder_csv(text)
