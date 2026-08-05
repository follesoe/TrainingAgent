"""Configuration loaded from the environment (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


@dataclass(frozen=True)
class Settings:
    api_key: str
    athlete_id: str

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env")

        api_key = os.getenv("INTERVALS_API_KEY", "").strip()
        if not api_key:
            raise ConfigError(
                "INTERVALS_API_KEY is not set. Copy .env.example to .env and add your "
                "API key from https://intervals.icu/settings (Developer Settings)."
            )

        # "0" is the intervals.icu shorthand for "the athlete owning this key".
        athlete_id = os.getenv("INTERVALS_ATHLETE_ID", "0").strip() or "0"
        return cls(api_key=api_key, athlete_id=athlete_id)


@dataclass(frozen=True)
class ZwiftSettings:
    """Zwift account credentials.

    Zwift issues no personal API keys, so the unofficial API needs the account
    password itself. See zwift.py for what that implies.
    """

    email: str
    password: str

    @classmethod
    def load(cls) -> "ZwiftSettings":
        load_dotenv(PROJECT_ROOT / ".env")

        email = os.getenv("ZWIFT_EMAIL", "").strip()
        password = os.getenv("ZWIFT_PASSWORD", "")
        if not email or not password:
            raise ConfigError(
                "ZWIFT_EMAIL and ZWIFT_PASSWORD are not both set. Add them to .env "
                "(see .env.example). Zwift has no personal API keys, so the unofficial "
                "API needs the account password. Accounts that sign in only through "
                "Apple/Google/Facebook have no password and cannot be used."
            )
        return cls(email=email, password=password)
