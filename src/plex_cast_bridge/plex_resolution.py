import random

from .ai_router import PlaybackIntent


def tags(item, name):
    return {str(getattr(tag, "tag", tag)) for tag in (getattr(item, name, None) or [])}


def resolve_artist(artist, target_artist: str | None):
    if not artist:
        raise ValueError(f"Artist '{target_artist}' not found in the Plex library")
    tracks = list(artist.tracks())
    if not tracks:
        raise ValueError(f"No tracks found for artist '{target_artist}'")
    random.shuffle(tracks)
    return tracks


def resolve_artist_radio(service, artist, intent: PlaybackIntent):
    if not artist:
        raise ValueError(f"Artist '{intent.target_artist}' not found in the Plex library")

    artist_tracks = list(artist.tracks())
    if not artist_tracks:
        raise ValueError(f"No tracks found for artist '{intent.target_artist}'")
    random.shuffle(artist_tracks)
    pinned_tracks = artist_tracks[:2]
    remaining_artist = artist_tracks[2:]

    peer_artists = [
        service._artist(name)
        for name in intent.seed_artists
        if name.casefold() != artist.title.casefold()
    ]
    peer_artists = [a for a in peer_artists if a is not None]
    peer_tracks = [t for a in peer_artists for t in a.tracks()]

    if len(peer_tracks) < 15:
        shared_genres = tags(artist, "genres")
        similar = tags(artist, "similar") | tags(artist, "similarArtists")
        fallback_artists = [
            a for a in service.artists
            if a != artist and (shared_genres.intersection(tags(a, "genres")) or a.title in similar)
        ]
        peer_tracks.extend([t for a in fallback_artists for t in a.tracks()])

    random.shuffle(remaining_artist)
    random.shuffle(peer_tracks)

    total_remaining = min(100 - len(pinned_tracks), len(remaining_artist) + len(peer_tracks))
    if total_remaining <= 0:
        return pinned_tracks

    artist_quota = min(len(remaining_artist), max(0, int(total_remaining * 0.50)))
    peer_quota = min(len(peer_tracks), total_remaining - artist_quota)

    selected_artist = remaining_artist[:artist_quota]
    selected_peers = peer_tracks[:peer_quota]

    queue = list(pinned_tracks)
    while selected_artist or selected_peers:
        if selected_peers:
            queue.append(selected_peers.pop(0))
        if selected_artist:
            queue.append(selected_artist.pop(0))

    return queue


def resolve_album(service, artist, intent: PlaybackIntent):
    if not intent.target_album:
        raise ValueError("Album name is required")
    albums = artist.albums() if artist else service.library.search(title=intent.target_album, libtype="album")
    album = next((a for a in albums if a.title.casefold() == intent.target_album.casefold()), None)
    if not album:
        raise ValueError("Album not found in the Plex library")
    return list(album.tracks())


def resolve_track(service, artist, intent: PlaybackIntent):
    if not intent.target_track:
        raise ValueError("Track name is required")
    tracks = artist.tracks() if artist else service.library.search(title=intent.target_track, libtype="track")
    matches = [t for t in tracks if t.title.casefold() == intent.target_track.casefold()]
    if intent.target_album:
        matches = [t for t in matches if (getattr(t, "parentTitle", "") or "").casefold() == intent.target_album.casefold()]
    return matches[:1]


def resolve_theme_mix(service, intent: PlaybackIntent):
    genres = {x.casefold() for x in intent.matched_genres}
    moods = {x.casefold() for x in intent.moods}
    seeds = {x.casefold() for x in intent.seed_artists}
    tracks = []
    for a in service.artists:
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


def resolve(service, intent: PlaybackIntent):
    artist = service._artist(intent.target_artist)
    if intent.mode == "artist":
        return resolve_artist(artist, intent.target_artist)
    if intent.mode == "artist_radio":
        return resolve_artist_radio(service, artist, intent)
    if intent.mode == "album":
        return resolve_album(service, artist, intent)
    if intent.mode == "track":
        return resolve_track(service, artist, intent)
    return resolve_theme_mix(service, intent)
