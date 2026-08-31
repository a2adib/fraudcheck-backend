"""
Adapter registry (FR-3.3).

The orchestrator asks this module which couriers exist; it never imports an adapter.
That indirection is what makes AC-3.4 true — a provider can be added, or swapped for a
mock, without a line changing anywhere downstream.
"""

import logging

from src.config import settings
from src.logistics.enums import ProviderEnum
from src.logistics.providers.base import CourierAdapter
from src.logistics.providers.mock import MockAdapter
from src.logistics.providers.pathao import PathaoAdapter

logger = logging.getLogger(__name__)

_LIVE_ADAPTERS: dict[ProviderEnum, CourierAdapter] = {}


def register(adapter: CourierAdapter) -> None:
    """Add (or replace) the live adapter for a provider."""
    _LIVE_ADAPTERS[adapter.name] = adapter


def unregister(provider: ProviderEnum) -> None:
    _LIVE_ADAPTERS.pop(provider, None)


def get_adapters() -> dict[ProviderEnum, CourierAdapter]:
    """
    Return the adapters a check should fan out to right now.

    In mock mode every provider is served by a ``MockAdapter`` — including ones with no
    live adapter written yet, so the demo shows the full three-provider fan-out
    (FR-11.1). Built per call rather than cached, so toggling ``MOCK_MODE`` or a
    provider timeout in a test takes effect immediately.
    """
    if settings.MOCK_MODE:
        return {provider: MockAdapter(provider) for provider in ProviderEnum}
    return dict(_LIVE_ADAPTERS)


register(PathaoAdapter())
