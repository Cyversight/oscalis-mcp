"""Runtime settings, read from environment variables with safe defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Transport = Literal["stdio", "sse"]

HARD_MAX_HOPS = 4
HARD_MAX_LIMIT = 100
MIN_RESPONSE_CHARS = 4_000
MAX_RESPONSE_CHARS = 1_000_000

# Upstream files reference catalogs and profiles by file name. Map those names to our catalog keys
# so profile imports resolve to the right local catalog. Operators can extend this mapping.
DEFAULT_HREF_ALIASES: dict[str, str] = {
    "NIST_SP-800-53_rev5_catalog.json": "800-53r5.2",
    "NIST_SP-800-53_rev5_catalog.xml": "800-53r5.2",
    "NIST_SP-800-53_rev5_LOW-baseline_profile.json": "800-53b-low",
    "NIST_SP-800-53_rev5_MODERATE-baseline_profile.json": "800-53b-moderate",
    "NIST_SP-800-53_rev5_HIGH-baseline_profile.json": "800-53b-high",
    "NIST_SP-800-53_rev5_PRIVACY-baseline_profile.json": "800-53b-privacy",
}


def _repo_root() -> Path:
    # src/oscalis/core/config.py -> repo root is three parents above "oscalis"
    return Path(__file__).resolve().parents[3]


def choose_data_dir(packaged: Path, repo_data: Path) -> Path:
    """The packaged data folder if it exists, else the source checkout's ``data/``."""
    return packaged if packaged.is_dir() else repo_data


def default_data_dir() -> Path:
    """Where the OSCAL data lives when ``OSCALIS_DATA_DIR`` is not set.

    A built wheel carries the data inside the package (``oscalis/data``), so an installed copy works from any directory
    on any platform. A source checkout, including an editable install, has no such folder and reads ``<repo>/data``.
    """
    return choose_data_dir(Path(__file__).resolve().parents[1] / "data", _repo_root() / "data")


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _float_env(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=default_data_dir)
    db_path: Path | None = None  # None -> in-memory index
    transport: Transport = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000
    max_hops: int = 3
    min_confidence: float = 0.30
    default_limit: int = 25
    max_limit: int = HARD_MAX_LIMIT
    max_full_items: int = 3
    # A list response is shortened (fewer items per page) to stay under this many characters. MCP clients cap one
    # tool result (Kiro: 30,000 characters); a page that is too big is cut off or rejected, so the agent loses it.
    max_response_chars: int = 24_000
    auth_token: str | None = field(default=None, repr=False)  # SSE bearer token; never printed
    href_aliases: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_HREF_ALIASES))

    @property
    def catalogs_dir(self) -> Path:
        return self.data_dir / "catalogs"

    @property
    def profiles_dir(self) -> Path:
        return self.data_dir / "profiles"

    @property
    def crosswalks_dir(self) -> Path:
        return self.data_dir / "crosswalks"

    @classmethod
    def from_env(cls) -> Settings:
        transport = os.environ.get("OSCALIS_TRANSPORT", "stdio")
        if transport not in ("stdio", "sse"):
            raise ValueError(f"OSCALIS_TRANSPORT must be 'stdio' or 'sse', got {transport!r}")
        data_dir = os.environ.get("OSCALIS_DATA_DIR")
        db_path = os.environ.get("OSCALIS_DB_PATH")
        defaults = cls()
        return cls(
            data_dir=Path(data_dir) if data_dir else defaults.data_dir,
            db_path=Path(db_path) if db_path else None,
            transport="sse" if transport == "sse" else "stdio",
            host=os.environ.get("OSCALIS_HOST", defaults.host),
            port=_int_env("OSCALIS_PORT", defaults.port, 1, 65535),
            max_hops=_int_env("OSCALIS_MAX_HOPS", defaults.max_hops, 1, HARD_MAX_HOPS),
            min_confidence=_float_env("OSCALIS_MIN_CONFIDENCE", defaults.min_confidence, 0.0, 1.0),
            default_limit=_int_env("OSCALIS_DEFAULT_LIMIT", defaults.default_limit, 1, HARD_MAX_LIMIT),
            max_limit=_int_env("OSCALIS_MAX_LIMIT", defaults.max_limit, 1, HARD_MAX_LIMIT),
            max_full_items=_int_env("OSCALIS_MAX_FULL_ITEMS", defaults.max_full_items, 1, HARD_MAX_LIMIT),
            max_response_chars=_int_env(
                "OSCALIS_MAX_RESPONSE_CHARS", defaults.max_response_chars, MIN_RESPONSE_CHARS, MAX_RESPONSE_CHARS
            ),
            auth_token=os.environ.get("OSCALIS_AUTH_TOKEN") or None,
        )
