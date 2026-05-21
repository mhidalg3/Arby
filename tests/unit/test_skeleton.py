"""Skeleton test that runs on Day 1 to verify the project is wired up.

This should pass as soon as `uv sync` completes and before any real code
is written. If this fails, your environment is broken.
"""

from __future__ import annotations


def test_can_import_config() -> None:
    """Configuration module loads without crashing."""
    from src import config

    assert hasattr(config, "get_settings")


def test_can_import_dutch_book() -> None:
    """Core arbitrage module loads."""
    from src.arbitrage import dutch_book

    assert hasattr(dutch_book, "detect_arbitrage")
    assert hasattr(dutch_book, "OddsQuote")
    assert hasattr(dutch_book, "ArbitrageOpportunity")


def test_can_import_storage_models() -> None:
    """Storage models load and Base is exported."""
    from src.storage import models

    assert hasattr(models, "Base")
    assert hasattr(models, "Match")
    assert hasattr(models, "Opportunity")
