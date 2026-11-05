import logging
import random
from collections import Counter

from plexapi.server import PlexServer

from .ai_router import PlaybackIntent

log = logging.getLogger(__name__)


def tags(item, name):
    return {str(getattr(tag, "tag", tag)) for tag in (getattr(item, name, None) or [])}


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

    def _artist(self, name):
        if not name:
            return None
        return next((a for a in self.artists if a.title.casefold() == name.casefold()), None)

    def resolve(self, intent: PlaybackIntent):
        artist = self._artist(intent.target_artist)
        if intent.mode == "artist":
            if not artist:
                raise ValueError(f"Artist '{intent.target_artist}' not found in the Plex library")
            tracks = list(artist.tracks())
            if not tracks:
                raise ValueError(f"No tracks found for artist '{intent.target_artist}'")
            random.shuffle(tracks)
            return tracks

        if intent.mode == "artist_radio":
            if not artist:
                raise ValueError(f"Artist '{intent.target_artist}' not found in the Plex library")

            # Pin Track 1: Always start with the requested target_artist
            artist_tracks = list(artist.tracks())
            if not artist_tracks:
                raise ValueError(f"No tracks found for artist '{intent.target_artist}'")
            random.shuffle(artist_tracks)
            first_track = artist_tracks.pop(0)
            remaining_artist = artist_tracks

            # Curated Pool: Drawn from Gemini's peer seed_artists (excluding target_artist)
            peer_artists = [
                self._artist(name)
                for name in intent.seed_artists
                if name.casefold() != artist.title.casefold()
            ]
            peer_artists = [a for a in peer_artists if a is not None]
            peer_tracks = [t for a in peer_artists for t in a.tracks()]

            # Fall back to Plex genre/similar tags only if seed_artists yield fewer than 15 tracks
            if len(peer_tracks) < 15:
                shared_genres = tags(artist, "genres")
                similar = tags(artist, "similar") | tags(artist, "similarArtists")
                fallback_artists = [
                    a for a in self.artists
                    if a != artist and (shared_genres.intersection(tags(a, "genres")) or a.title in similar)
                ]
                peer_tracks.extend([t for a in fallback_artists for t in a.tracks()])

            # Weighted Interleaving: ~25% target_artist, ~75% seed_artists (cap at ~100 tracks)
            random.shuffle(remaining_artist)
            random.shuffle(peer_tracks)

            total_remaining = min(99, len(remaining_artist) + len(peer_tracks))
            if total_remaining == 0:
                return [first_track]

            artist_quota = min(len(remaining_artist), max(0, int(total_remaining * 0.25)))
            peer_quota = min(len(peer_tracks), total_remaining - artist_quota)

            selected_artist = remaining_artist[:artist_quota]
            selected_peers = peer_tracks[:peer_quota]

            # Interleave: 1 target artist track every ~3 peer tracks
            queue = [first_track]
            while selected_artist or selected_peers:
                for _ in range(3):
                    if selected_peers:
                        queue.append(selected_peers.pop(0))
                if selected_artist:
                    queue.append(selected_artist.pop(0))

            return queue
        if intent.mode == "album":
            if not intent.target_album:
                raise ValueError("Album name is required")
            albums = artist.albums() if artist else self.library.search(title=intent.target_album, libtype="album")
            album = next((a for a in albums if a.title.casefold() == intent.target_album.casefold()), None)
            if not album:
                raise ValueError("Album not found in the Plex library")
            return list(album.tracks())
        if intent.mode == "track":
            if not intent.target_track:
                raise ValueError("Track name is required")
            tracks = artist.tracks() if artist else self.library.search(title=intent.target_track, libtype="track")
            matches = [t for t in tracks if t.title.casefold() == intent.target_track.casefold()]
            if intent.target_album:
                matches = [t for t in matches if (getattr(t, "parentTitle", "") or "").casefold() == intent.target_album.casefold()]
            return matches[:1]
        genres = {x.casefold() for x in intent.matched_genres}
        moods = {x.casefold() for x in intent.moods}
        seeds = {x.casefold() for x in intent.seed_artists}
        tracks = []
        for a in self.artists:
            artist_genres = {x.casefold() for x in tags(a, "genres")}
            artist_moods = {x.casefold() for x in tags(a, "moods")}
            for album in a.albums():
                album_genres = artist_genres | {x.casefold() for x in tags(album, "genres")}
                album_moods = artist_moods | {x.casefold() for x in tags(album, "moods")}
                year = getattr(album, "year", None)
                if (seeds or genres) and a.title.casefold() not in seeds and not genres.intersection(album_genres):
                    continue
                if moods and not moods.intersection(album_moods) and a.title.casefold() not in seeds:
                    continue
                if intent.decades and (not year or not any(d <= year < d + 10 for d in intent.decades)):
                    continue
                tracks.extend(album.tracks())
        random.shuffle(tracks)
        return tracks
