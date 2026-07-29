# Feature: Local Buffer

## Problem

Currently, Music Assistant will only buffer about 30s of audio due to unclear
legal constraints. 30s is considered safe / legal because streaming providers
such as Apple Music, Deezer and others also provide snippets of 30s length to
the public internet. But not every media item in Music Assistant has legal
constraints by streaming providers. Local media library, podcasts, etc. all
don't have this legal restriction but suffer from the same technical constraints.
If the connection drops for more than 30s, the stream is stopped and only resumed
if the network comes back.

## Analysis — Four Independent Bottlenecks

Tracing the full playback pipeline reveals **four separate rate-limiting
mechanisms**, one per player type, and **none of them are provider-aware**
today. The 30-second effect the user experiences comes from different hardcoded
constants depending on which player they use.

### Player delivery types

| Player type | How audio reaches the player | Examples |
|---|---|---|
| **SendSpin** | MA pushes PCM F32LE → PushStream → encodes to per-client codec (FLAC/Opus) → WebSocket/WebRTC | Mobile app, web app, Chromecast bridge, AirPlay bridge, Local Audio bridge |
| **HTTP flow** | MA gets PCM → ffmpeg encodes to FLAC/MP3 → HTTP response → player fetches URL | Sonos, DLNA |
| **Snapcast** | MA gets PCM → ffmpeg → Snapcast TCP source → Snapcast server → Snapclient | Snapcast speakers |
| **AirPlay** | MA gets PCM → cliraop/cliap2 binary → AirPlay device | AirPlay speakers |

### Bottleneck 1: SendSpin — `_PRODUCER_BUFFER_LIMIT_US = 30s`

**File:** `music_assistant/providers/sendspin/playback.py:72`

```python
_PRODUCER_BUFFER_LIMIT_US = 30_000_000  # 30 seconds
```

**Pipeline:**
```
MA get_stream() PCM F32LE
  → _produce_pending_chunks() slices PCM into 100ms chunks
    → asyncio.Queue (max 64 chunks = 6.4s)
      → _commit_pending_chunks()
        → push_stream.prepare_audio(pcm, format)
        → push_stream.commit_audio()
        → push_stream.sleep_to_limit_buffer(30s)   ← BACKPRESSURE
```

**Used at:**
- Line 865: `push_stream.sleep_to_limit_buffer(_PRODUCER_BUFFER_LIMIT_US)`
- Line 551: Join-catchup queue sizing.
- Line 1419: Buffer drain safety timeout.

**Impact:** The mobile app can only ever be 30s ahead of playback. A network
dropout longer than 30s exhausts the buffer and playback stops.

### Bottleneck 2: HTTP flow — ffmpeg `-readrate 1.1`

**File:** `music_assistant/controllers/streams/controller.py:1025`

```python
extra_input_args=["-readrate", "1.1", "-readrate_initial_burst", "5"],
```

**Impact:** Sonos/DLNA players receive data at 1.1× realtime, so they can only
be ~3-10s ahead. A network dropout of ~30s exhausts that in seconds.

### Bottleneck 3: Snapcast — `buffered(30)` queue

**File:** `music_assistant/controllers/streams/controller.py:1288-1289`

```python
return buffered(flow_stream, buffer_size=30, min_buffer_before_yield=1)
```

### Bottleneck 4: AirPlay — `buffered(30)` queue

Same `buffered(30)` path as Snapcast.

## Solution: Per-Item Dynamic Buffer

Make **all four bottlenecks** respect a per-item, three-layer model:

```
Provider says:   "I can supply X seconds ahead"   (per provider type)
Player says:     "My hardware can safely hold Y"   (per player type)
Server computes: min(X or 30, Y or 600, ABS_MAX=600)
                 → applies per-item, per-protocol
```

**Key principle:** Each track gets its own buffer limit based on its own
provider. A Spotify track → 30s. The next local FLAC → 300s. The next
Spotify track → 30s again. Clean separation because SendSpin starts a new
stream for every track.

### Design constraints

- `StreamDetails` and `PlayerFeature` live in the external
  `music-assistant-models` pip package — cannot be modified.
- Provider preference flows through MA's `MusicProvider` base class.
- Player cap flows through MA's `Player` base class.
- The translation to specific mechanisms happens per-player-type in their
  respective code paths.

### Layer 1: `MusicProvider.buffer_preference_seconds`

**File:** `music_assistant/models/music_provider.py`

Add property:

```python
@property
def buffer_preference_seconds(self) -> int | None:
    """
    Preferred client-side buffer in seconds for this provider's content.

    Controls how much encoded audio the server attempts to keep in the
    player's buffer ahead of playback. The server translates this into
    the appropriate mechanism per player type (SendSpin push-ahead limit,
    HTTP ffmpeg readrate, Snapcast/AirPlay buffered queue size).

    * ``None`` (default) — use server default (~30s effective).
      Suitable for streaming providers with legal/business restrictions.
    * ``0`` — no provider-side limit. Allow the player to buffer the
      entire file (subject to the player's hardware cap).
      Suitable for local media, podcasts, audiobooks.
    * ``N`` (>0) — explicit target in seconds.

    Override this in provider subclasses to signal buffering requirements.
    """
    return None
```

| Provider | Override | Rationale |
|---|---|---|
| `filesystem_local` | `0` | Local files have no legal constraints |
| `filesystem_smb` | `0` | No legal constraints |
| `filesystem_google_drive` | `0` | Self-hosted content |
| `filesystem_onedrive` | `0` | Self-hosted content |
| `filesystem_nfs` | `0` | Self-hosted content |
| `jellyfin` | `0` | Self-hosted media |
| `plex` | `0` | Self-hosted media |
| `subsonic` | `0` | Self-hosted media |
| `audiobookshelf` | `0` | Self-hosted content |
| `podcastfeed` | `0` | Publicly available |
| `radiobrowser` | `0` | Publicly available |
| All streaming providers | Keep default `None` | No behaviour change |

### Layer 2: `Player.max_client_buffer_seconds`

**File:** `music_assistant/models/player.py`

Add property:

```python
@property
def max_client_buffer_seconds(self) -> int | None:
    """
    Maximum safe client-side buffer in seconds for this player hardware.

    If the device is known to choke on large audio buffers (e.g. Chromecast
    OOM issues), override this to a low value. The server will never exceed
    this cap when computing the effective buffer target, regardless of what
    the provider requests.

    * ``None`` (default) — unknown, use a conservative server default (30s).
    * ``N`` — hard cap in seconds.

    Override this in player provider subclasses to signal hardware limits.
    """
    return None
```

| Player type | Cap | Rationale |
|---|---|---|
| **SendSpin mobile/web** | `300` (5 min) | Modern devices handle this easily |
| **SendSpin Chromecast bridge** | `15` | Cast SDK OOM issues |
| **Snapcast** | `300` | Has own server-side buffer config |
| **AirPlay** | `300` | Has own latency config |
| **Sonos** | `None` (default → 30s) | Conservative |
| **DLNA** | `None` (default → 30s) | Conservative |

### Layer 3: Per-protocol translation

Each protocol gets its effective limit per **individual stream/track**:

```
effective = min(
    provider_preference or 30s,  ← from Layer 1 (per-item)
    player_cap or 600s,         ← from Layer 2
    600s                        ← absolute max
)
```

| Protocol | When evaluated | Mechanism |
|----------|---------------|-----------|
| **SendSpin** | Per-stream (each track = new stream) | `_PRODUCER_BUFFER_LIMIT_US` set at stream init, fixed for that track |
| **HTTP flow** | Per-flow-stream (each queue play starts a flow) | ffmpeg `-readrate` set at encode start |
| **Snapcast/AirPlay** | Per-buffered-queue | `buffered(n)` queue size |
| **AudioBuffer** | Per-stream PCM buffer | capacity adjusted per streamdetails |

**No "most permissive wins" anywhere.** Each track gets its own limit based on
its own provider. Since SendSpin starts a new stream for every track, the
buffer limit is inherently per-item. When one track ends and the next begins,
the old stream ends, client buffer is discarded, and the new stream starts
fresh with the correct limit.

### Crossfade Handling

Crossfade means a single continuous stream spans two tracks. The buffer limit
can't switch mid-stream at the exact crossfade point.

| When | Buffer limit used | Why |
|------|-------------------|-----|
| Queue sizing | `max(current_item, next_item)` | Ensure queue is large enough for overlap |
| During playback | Current item's provider | Dynamic per-item |
| During crossfade | Outgoing item's provider | Current item hasn't switched yet |
| After crossfade completes | New current item's provider | `current_item` switches, limit follows |

**Queue sizing** (set once at session start): Use the **maximum** of current
and next track's limits. This ensures the queue is large enough to hold both
tracks' PCM during the overlap. Since each SendSpin stream is a single track,
crossfade within a stream is relatively rare in practice.

**Runtime backpressure** (checked every 100ms): Use the **current track's**
provider limit. When `current_item` transitions (after crossfade completes),
the limit naturally follows.

**TOS concern:** When transitioning from a local file (300s limit) to a
streaming service track (30s limit), the client could theoretically hold
pre-filled streaming audio in its buffer. In practice:
- The server only fetched ~30s from the streaming provider's API (governed by
  `buffer_preference_seconds`)
- The `sleep_to_limit_buffer()` mechanism throttles encoded output to match
  the current item's limit
- Once the current item switches to the streaming track, the encoded push
  rate drops to 30s, so the client never accumulates more than ~30s of
  encoded streaming audio

### Implementation order

| Step | File | Change |
|------|------|--------|
| 1 | `music_assistant/models/music_provider.py` | Add `buffer_preference_seconds` property (default `None`) |
| 2 | `music_assistant/models/player.py` | Add `max_client_buffer_seconds` property (default `None`) |
| 3 | `music_assistant/controllers/streams/controller.py` | Add `_calc_flow_readrate()` + `_calc_buffered_queue_size()`; modify lines 1025 and 1289 |
| 4 | `music_assistant/controllers/streams/audio_buffer.py` | Apply provider preference to AudioBuffer capacity |
| 5 | `music_assistant/providers/sendspin/playback.py` | Add `_compute_buffer_limit_us()`; replace `_PRODUCER_BUFFER_LIMIT_US` at lines 551, 865, 1419 |
| 6 | `music_assistant/providers/filesystem_local/__init__.py` | Override `buffer_preference_seconds → 0` |
| 7 | `music_assistant/providers/jellyfin/__init__.py` | Override `buffer_preference_seconds → 0` |
| 8 | `music_assistant/providers/plex/__init__.py` | Override `buffer_preference_seconds → 0` |
| 9 | `music_assistant/providers/subsonic/__init__.py` | Override `buffer_preference_seconds → 0` |
| 10 | `music_assistant/providers/sendspin/player.py` | Override `max_client_buffer_seconds → 300` |
| 11 | `music_assistant/providers/snapcast/player.py` | Override `max_client_buffer_seconds → 300` |
| 12 | `music_assistant/providers/airplay/player.py` | Override `max_client_buffer_seconds → 300` |
| 13 | `music_assistant/providers/chromecast/sendspin_bridge.py` | Override `max_client_buffer_seconds → 15` |

### Mixed queue example

```
Queue: Spotify (30s) → Local FLAC (300s) → Spotify (30s)

Stream 1 (Spotify):
  Limit = 30s → push_rate ~1× realtime → client buffer ~30s

Stream 2 (Local FLAC):
  Limit = 300s → push_rate ~10× realtime → client buffer fills to 300s
  → survives 5 min network dropout

Stream 3 (Spotify):
  Limit = 30s → push_rate ~1× realtime → client buffer ~30s
  → server only fetched ~30s from Spotify API
```

### Backwards Compatibility

- Existing providers keep default `None` → 30s → no behaviour change
- Existing players keep default `None` → 30s → no behaviour change
- The `_PRODUCER_BACKLOG_SIZE` (64 chunks = 6.4s) is unchanged

### Example scenarios

| Provider | Player | target_s | HTTP readrate | SendSpin limit | Effect |
|---|---|---|---|---|---|
| Spotify | Sonos | 30 (default) | 1.1× | 30s | Same as today |
| Spotify | Chromecast | 15 (player cap) | 1.1× | 15s | Slightly less than today (safe) |
| Local file | Sonos | 600 (abs max) | 10.0× | 600s | 10 min buffered in ~60s |
| Local file | Chromecast | 15 (player cap) | 1.1× | 15s | Chromecast protected from OOM |
| Local file | SendSpin mobile | 300 (player cap) | N/A | 300s | 5 min buffered via WebSocket |
| Podcast | SendSpin mobile | 300 (player cap) | N/A | 300s | Survives 5 min dropout |

### Risks

| Risk | Mitigation |
|---|---|
| **Chromecast OOM** with large buffer | Chromecast player cap = 15s, enforced regardless of provider preference |
| **SendSpin mobile OOM** with 300s buffer | 300s of Opus @ 160kbps = ~6MB. Manageable on modern phones. WebSocket has backpressure. |
| **Skip/next latency** with large client buffer | On `play_index()`, the stream session is cancelled and a new one starts. The player receives a fresh stream and discards its old buffer. |
| **ffmpeg CPU overload** at readrate 10× | FLAC encode at 44100/16/2 runs at ~50× on modern hardware. readrate=10 is easily sustained. |

### Tests

| Test | Scope |
|---|---|
| `test_provider_pref_default` | `None` → target = 30s default |
| `test_provider_pref_unlimited` | `0` → target = min(600, player_cap) |
| `test_provider_pref_explicit` | `120` → target = min(120, player_cap) |
| `test_player_cap_limits_provider` | Chromecast cap 15s limits even unlimited provider |
| `test_per_item_different_limits` | Two consecutive tracks with different providers get different limits |
| `test_http_readrate_calc` | target→readrate conversion matches formula |
| `test_sendspin_buffer_limit_us` | `_compute_buffer_limit_us` returns correct µs |
| `test_buffered_queue_size` | `_calc_buffered_queue_size` returns correct chunks |
| `test_audio_buffer_capacity` | AudioBuffer adjusts max_size_seconds from provider pref |
| `test_filesystem_local_override` | `buffer_preference_seconds` returns `0` |
| `test_sendspin_player_cap` | `max_client_buffer_seconds` returns `300` |
| `test_chromecast_bridge_cap` | Chromecast bridge returns `15` |
| `test_crossfade_queue_sizing` | Crossfaded queue uses `max(current, next)` for queue sizing |
