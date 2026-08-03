"""Training agent: analyse endurance training data from intervals.icu."""

from .config import Settings
from .intervals import IntervalsClient

__all__ = ["IntervalsClient", "Settings"]
