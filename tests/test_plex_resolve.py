from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plex_cast_bridge.ai_router import PlaybackIntent
from plex_cast_bridge.plex_resolution import (
    resolve_album,
    resolve_artist,
    resolve_artist_radio,
    resolve_theme_mix,
    resolve_track,
)
from plex_cast_bridge.plex_service import PlexService


def make_track(title, parent="Album", grandparent="Artist"):
    return SimpleNamespace(title=title, parentTitle=parent, grandparentTitle=grandparent)


def tag_list(names):
    return [SimpleNamespace(tag=n) for n in names]


def make_album(title, tracks, year=None, genres=None, moods=None):
    genre_objs = tag_list(genres or [])
    mood_objs = tag_list(moods or [])
    return SimpleNamespace(
        title=title,
        year=year,
        genres=genre_objs,
        moods=mood_objs,
        tracks=lambda t=tracks: list(t),
    )


def make_artist(title, tracks=None, albums=None, genres=None, moods=None, similar=None):
    track_list = tracks or []
    album_list = albums if albums is not None else []
    return SimpleNamespace(
        title=title,
        genres=tag_list(genres or []),
        moods=tag_list(moods or []),
        similar=similar or [],
        similarArtists=[],
        tracks=lambda: list(track_list),
        albums=lambda: list(album_list),
    )


def bare_service(artists, library_search=None):
    service = PlexService.__new__(PlexService)
    service.artists = artists
    service.library = SimpleNamespace(search=library_search or MagicMock(return_value=[]))
    return service


@pytest.mark.parametrize(
    "call",
    [
        lambda: resolve_artist(None, "Missing"),
        lambda: resolve_artist_radio(
            bare_service([]),
            None,
            PlaybackIntent(mode="artist_radio", target_artist="Missing"),
        ),
    ],
)
def test_missing_artist_raises(call):
    with pytest.raises(ValueError, match="not found"):
        call()


def test_artist_mode_shuffles_and_returns_all_tracks(monkeypatch):
    tracks = [make_track(f"t{i}") for i in range(4)]
    artist = make_artist("Band", tracks=tracks)
    monkeypatch.setattr("plex_cast_bridge.plex_resolution.random.shuffle", lambda xs: xs.reverse())
    result = resolve_artist(artist, "Band")
    assert result == list(reversed(tracks))


def test_artist_empty_tracks_raises():
    artist = make_artist("Empty", tracks=[])
    with pytest.raises(ValueError, match="No tracks found"):
        resolve_artist(artist, "Empty")


def test_artist_radio_pins_two_target_tracks():
    tracks = [make_track(f"a{i}") for i in range(6)]
    peer_tracks = [make_track(f"p{i}") for i in range(20)]
    target = make_artist("Target", tracks=tracks)
    peer = make_artist("Peer", tracks=peer_tracks)
    svc = bare_service([target, peer])
    intent = PlaybackIntent(mode="artist_radio", target_artist="Target", seed_artists=["Peer"])
    queue = resolve_artist_radio(svc, target, intent)
    assert len(queue) >= 2
    assert queue[0].title.startswith("a")
    assert queue[1].title.startswith("a")


def test_album_mode_with_artist():
    song = make_track("Only")
    album = make_album("Greatest", [song])
    artist = make_artist("Band", albums=[album])
    svc = bare_service([artist])
    result = resolve_album(svc, artist, PlaybackIntent(mode="album", target_album="Greatest"))
    assert result == [song]


def test_album_mode_library_search_without_artist():
    song = make_track("Lib")
    album = make_album("Remote", [song])
    svc = bare_service([], library_search=MagicMock(return_value=[album]))
    result = resolve_album(svc, None, PlaybackIntent(mode="album", target_album="Remote"))
    assert result == [song]


def test_album_not_found_raises():
    artist = make_artist("Band", albums=[make_album("Other", [])])
    svc = bare_service([artist])
    with pytest.raises(ValueError, match="Album not found"):
        resolve_album(svc, artist, PlaybackIntent(mode="album", target_album="Missing"))


def test_album_name_required_raises():
    svc = bare_service([])
    with pytest.raises(ValueError, match="Album name is required"):
        resolve_album(svc, None, PlaybackIntent(mode="album", target_album=None))


def test_track_mode_filters_by_album():
    t1 = make_track("Song", parent="Vol1")
    t2 = make_track("Song", parent="Vol2")
    artist = make_artist("Band", tracks=[t1, t2])
    svc = bare_service([artist])
    result = resolve_track(
        svc,
        artist,
        PlaybackIntent(mode="track", target_track="Song", target_album="Vol2"),
    )
    assert result == [t2]


def test_track_name_required_raises():
    svc = bare_service([])
    with pytest.raises(ValueError, match="Track name is required"):
        resolve_track(svc, None, PlaybackIntent(mode="track", target_track=None))


@pytest.mark.parametrize(
    "intent_kwargs,expected_titles",
    [
        (
            {"mode": "theme_mix", "matched_genres": ["Rock"]},
            {"rock_hit"},
        ),
        (
            {"mode": "theme_mix", "moods": ["Chill"]},
            {"chill_song"},
        ),
        (
            {"mode": "theme_mix", "seed_artists": ["Seeded"]},
            {"seed_track"},
        ),
    ],
)
def test_theme_mix_genre_mood_seed(intent_kwargs, expected_titles, monkeypatch):
    rock = make_track("rock_hit")
    chill = make_track("chill_song")
    seed = make_track("seed_track")
    a1 = make_artist(
        "Rocker",
        albums=[make_album("R", [rock], genres=["Rock"])],
        genres=["Rock"],
    )
    a2 = make_artist(
        "Lounger",
        albums=[make_album("L", [chill], moods=["Chill"])],
        moods=["Chill"],
    )
    a3 = make_artist("Seeded", albums=[make_album("S", [seed])])
    svc = bare_service([a1, a2, a3])
    monkeypatch.setattr("plex_cast_bridge.plex_resolution.random.shuffle", lambda xs: None)
    tracks = resolve_theme_mix(svc, PlaybackIntent(**intent_kwargs))
    titles = {t.title for t in tracks}
    assert expected_titles <= titles


def test_theme_mix_decade_filter(monkeypatch):
    old = make_track("eighty")
    new = make_track("two_thou")
    albums = [
        make_album("80s", [old], year=1985, genres=["Pop"]),
        make_album("00s", [new], year=2005, genres=["Pop"]),
    ]
    artist = make_artist("Timeline", albums=albums, genres=["Pop"])
    svc = bare_service([artist])
    monkeypatch.setattr("plex_cast_bridge.plex_resolution.random.shuffle", lambda xs: None)
    tracks = resolve_theme_mix(
        svc,
        PlaybackIntent(mode="theme_mix", matched_genres=["Pop"], decades=[1980]),
    )
    assert {t.title for t in tracks} == {"eighty"}


def test_plex_service_resolve_delegates():
    song = make_track("delegated")
    artist = make_artist("Band", tracks=[song])
    svc = bare_service([artist])
    svc._artist = PlexService._artist.__get__(svc, PlexService)
    result = svc.resolve(PlaybackIntent(mode="artist", target_artist="Band"))
    assert len(result) == 1
    assert result[0].title == "delegated"
