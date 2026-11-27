from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET

import pytest
from plexapi.audio import Artist
from plexapi.library import MusicSection

from plex_cast_bridge.voice_cold_start import VoiceColdStart


class FakePlexServer:
    def __init__(self, total_tracks=60):
        self.total_tracks = total_tracks
        self.requests = []

    def query(self, path, headers=None, params=None):
        headers = headers or {}
        self.requests.append((path, dict(headers), params))
        start = int(headers.get("X-Plex-Container-Start", 0))
        size = int(headers.get("X-Plex-Container-Size", 100))
        count = min(size, max(0, self.total_tracks - start))
        root = ET.Element("MediaContainer", size=str(count), totalSize=str(self.total_tracks))
        for index in range(start, start + count):
            ET.SubElement(
                root,
                "Track",
                type="track",
                ratingKey=str(index + 1),
                key=f"/library/metadata/{index + 1}",
                title=f"Track {index + 1}",
            )
        return root


def service():
    return VoiceColdStart(cast=None, plex_provider=lambda: None)


def test_artist_selection_uses_actual_plexapi_artist_tracks_with_server_limit():
    server = FakePlexServer()
    artist = Artist(
        server,
        ET.fromstring('<Directory title="DJ Shadow" key="/library/metadata/77" ratingKey="77"/>'),
    )
    plex = SimpleNamespace(artists=[artist])
    tracks = service()._selection(
        plex,
        {"voice_cold_start_selection": "artist", "voice_cold_start_artist": "dj shadow"},
    )
    assert len(tracks) == 25
    path, headers, _params = server.requests[0]
    assert path.startswith("/library/metadata/77/allLeaves")
    assert headers["X-Plex-Container-Size"] == "25"
    assert len({track.ratingKey for track in tracks}) == 25
    assert {track.title for track in tracks} <= {f"Track {index}" for index in range(1, 26)}


@pytest.mark.parametrize("artists", [
    [],
    [SimpleNamespace(title="DJ Shadows")],
    [SimpleNamespace(title="DJ Shadow"), SimpleNamespace(title="DJ Shadow")],
])
def test_artist_selection_rejects_missing_or_ambiguous_artists(artists):
    # Deliberately do not offer a tracks() method: unresolved names must fail before lookup.
    plex = SimpleNamespace(artists=artists)
    with pytest.raises(ValueError, match="missing or ambiguous"):
        service()._selection(
            plex,
            {"voice_cold_start_selection": "artist", "voice_cold_start_artist": "DJ Shadow"},
        )


def test_random_library_selection_uses_actual_plexapi_query_and_limit():
    server = FakePlexServer()
    library = MusicSection(
        server,
        ET.fromstring('<Directory key="1" title="Music" type="artist"/>'),
    )
    library.listSorts = lambda libtype=None: [SimpleNamespace(key="random")]
    plex = SimpleNamespace(library=library)
    tracks = service()._selection(plex, {"voice_cold_start_selection": "shuffle"})
    assert len(tracks) == 25
    path, headers, _params = server.requests[0]
    assert path.startswith("/library/sections/1/all?")
    query = parse_qs(urlsplit(path).query)
    assert query["sort"] == ["track.random"]
    assert query["type"] == ["10"]
    assert headers["X-Plex-Container-Size"] == "25"
