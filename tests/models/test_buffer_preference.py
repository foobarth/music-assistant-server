"""Tests for the per-provider buffer preference model."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

from music_assistant_models.enums import ProviderType

from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.player import Player
from music_assistant.providers.sendspin.playback import _compute_effective_buffer_us


# ── MusicProvider ──────────────────────────────────────────────────────────


def test_music_provider_buffer_preference_default() -> None:
    """The base MusicProvider returns None (use default 30s)."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.type = ProviderType.MUSIC
    manifest.domain = "test"
    config = MagicMock()
    config.name = "Test"
    config.instance_id = "test--1"
    config.get_value = MagicMock(return_value="GLOBAL")
    provider = MusicProvider(mass, manifest, config, supported_features=set())

    assert provider.buffer_preference_seconds is None


# ── Player ─────────────────────────────────────────────────────────────────


def test_player_max_client_buffer_default() -> None:
    """The base Player returns None (use default 30s)."""
    prov = MagicMock()
    player = Player(
        player_id="test_player",
        provider=prov,
    )
    assert player.max_client_buffer_seconds is None


# ── _compute_effective_buffer_us ───────────────────────────────────────────


def _make_player_with_mocks(
    provider_pref: int | None = None,
    player_cap: int | None = None,
    provider_domain: str = "filesystem_local",
) -> MagicMock:
    """Create a mocked player with controlled provider and cap."""
    mass = MagicMock()
    provider = MagicMock()
    provider.buffer_preference_seconds = provider_pref
    mass.get_provider.return_value = provider

    queue = MagicMock()
    item = MagicMock()
    item.provider = f"{provider_domain}--test"
    queue.current_item = item
    mass.player_queues = MagicMock()

    player = MagicMock()
    player.mass = mass
    player.active_queue = queue
    type(player).max_client_buffer_seconds = PropertyMock(return_value=player_cap)
    return player


def test_compute_buffer_default() -> None:
    """No provider pref and no player cap -> 30s default."""
    player = _make_player_with_mocks(provider_pref=None, player_cap=None)
    result = _compute_effective_buffer_us(player)
    assert result == 30_000_000


def test_compute_buffer_unlimited_provider() -> None:
    """Provider pref = 0 (unlimited), no player cap -> 600s absolute max."""
    player = _make_player_with_mocks(provider_pref=0, player_cap=None)
    result = _compute_effective_buffer_us(player)
    assert result == 600_000_000


def test_compute_buffer_explicit_provider_pref() -> None:
    """Provider pref = 120, no player cap -> 120s."""
    player = _make_player_with_mocks(provider_pref=120, player_cap=None)
    result = _compute_effective_buffer_us(player)
    assert result == 120_000_000


def test_compute_buffer_player_cap_limits_provider() -> None:
    """Provider pref unlimited (0), player cap = 15 -> 15s."""
    player = _make_player_with_mocks(provider_pref=0, player_cap=15)
    result = _compute_effective_buffer_us(player)
    assert result == 15_000_000


def test_compute_buffer_player_cap_limits_unlimited() -> None:
    """Provider pref = 300, player cap = 30 -> 30s (player cap wins)."""
    player = _make_player_with_mocks(provider_pref=300, player_cap=30)
    result = _compute_effective_buffer_us(player)
    assert result == 30_000_000


def test_compute_buffer_no_active_queue() -> None:
    """No active queue on player -> fall back to 30s default."""
    player = MagicMock()
    player.active_queue = None
    type(player).max_client_buffer_seconds = PropertyMock(return_value=None)
    result = _compute_effective_buffer_us(player)
    assert result == 30_000_000


def test_compute_buffer_no_current_item() -> None:
    """Active queue but no current item -> fall back to 30s default."""
    mass = MagicMock()
    queue = MagicMock()
    queue.current_item = None
    player = MagicMock()
    player.mass = mass
    player.active_queue = queue
    type(player).max_client_buffer_seconds = PropertyMock(return_value=None)
    result = _compute_effective_buffer_us(player)
    assert result == 30_000_000


def test_compute_buffer_streaming_provider() -> None:
    """Streaming provider (None) with player cap = 300 -> 30s default."""
    player = _make_player_with_mocks(
        provider_pref=None, player_cap=300, provider_domain="spotify"
    )
    result = _compute_effective_buffer_us(player)
    assert result == 30_000_000
