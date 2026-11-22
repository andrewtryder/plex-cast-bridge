import logging
from collections import Counter

from plexapi.server import PlexServer

from .ai_router import PlaybackIntent
from .plex_resolution import resolve as resolve_playback_intent, tags

log = logging.getLogger(__name__)


class PlexService:
    def __init__(self, url, token, section):
        self.server = PlexServer(url, token)
        self.library = self.server.library.section(section)
        self.artists = []
        self.catalog = {}

    def refresh(self):
        artists = self.library.all(libtype="artist")
        genres, moods = Counter(), Counter()
        albums = set()
        for artist in artists:
            genres.update(tags(artist, "genres"))
            moods.update(tags(artist, "moods"))
        # Albums supply more complete genre/mood metadata than artists alone.
        for album in self.library.all(libtype="album"):
            albums.add(album.title)
            genres.update(tags(album, "genres"))
            moods.update(tags(album, "moods"))
        self.artists = artists
        self.catalog = {"artists": sorted(a.title for a in artists), "albums": sorted(albums),
                        "genres": sorted(genres), "moods": sorted(moods)}
        return self.catalog

    def library_summary(self) -> dict:
        """Cheap counts for Settings → Playback (artists/albums from cache; tracks via size query)."""
        section_title = getattr(self.library, "title", None) or str(getattr(self.library, "key", "Music"))
        if not self.catalog:
            try:
                self.refresh()
            except Exception as exc:
                log.warning("library_summary refresh failed: %s", exc)
                return {
                    "linked": True,
                    "section": section_title,
                    "artists": None,
                    "albums": None,
                    "tracks": None,
                }
        artists = len(self.catalog.get("artists") or [])
        albums = len(self.catalog.get("albums") or [])
        tracks = None
        try:
            # type=10 is track; request size only (no payload).
            el = self.server.query(
                f"/library/sections/{self.library.key}/all",
                params={"type": 10, "X-Plex-Container-Start": 0, "X-Plex-Container-Size": 0},
            )
            raw = el.attrib.get("totalSize") if hasattr(el, "attrib") else None
            if raw is None and isinstance(el, dict):
                raw = el.get("totalSize")
            if raw is not None:
                tracks = int(raw)
        except Exception as exc:
            log.debug("track count unavailable: %s", exc)
        return {
            "linked": True,
            "section": section_title,
            "artists": artists,
            "albums": albums,
            "tracks": tracks,
        }

    def _artist(self, name):
        if not name:
            return None
        return next((a for a in self.artists if a.title.casefold() == name.casefold()), None)

    def resolve(self, intent: PlaybackIntent):
        return resolve_playback_intent(self, intent)
