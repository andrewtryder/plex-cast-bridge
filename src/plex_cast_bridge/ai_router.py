import json
import logging
import re
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

CANDIDATE_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.1-flash-lite",
]

RADIO_KEYWORDS = (
    "radio",
    "station",
    "mix",
    "similar to",
    "like ",
)


def _normalize_text(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


class PlaybackIntent(BaseModel):
    mode: Literal["artist", "artist_radio", "album", "track", "theme_mix"]
    target_artist: str | None = None
    target_album: str | None = None
    target_track: str | None = None
    matched_genres: list[str] = Field(default_factory=list)
    seed_artists: list[str] = Field(default_factory=list, max_length=10)
    decades: list[int] = Field(default_factory=list, description="Decade start years, e.g. 1990")
    moods: list[str] = Field(default_factory=list)
    target_speaker: str | None = None


class SmartDJ:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client: genai.Client | None = genai.Client(api_key=api_key) if api_key else None
        self._config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=PlaybackIntent,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    def _try_fast_artist_match(self, query: str, artists: list[str]) -> PlaybackIntent | None:
        """
        0ms deterministic fast-path: if the query directly names an artist in the catalog
        (without asking for a radio/station/mix/album/track), immediately return mode='artist'.
        """
        raw_lower = query.lower().strip()
        if any(kw in raw_lower for kw in RADIO_KEYWORDS):
            return None
        if any(w in raw_lower for w in ("album", "track", "song")):
            return None

        # Strip common leading command verbs and trailing modifiers
        stripped = re.sub(
            r"^(?:hey google\s+)?(?:please\s+)?(?:play|switch to|listen to|put on|start|queue)(?:\s+only|\s+some|\s+tracks by|\s+songs by|\s+music by)?\s+",
            "",
            raw_lower,
        )
        stripped = re.sub(r"\s+(?:only|on plex|from plex)$", "", stripped).strip()
        norm_query = _normalize_text(stripped)
        if not norm_query:
            return None

        for artist in artists:
            norm_artist = _normalize_text(artist)
            # Check exact normalized match or leading-article stripped match (e.g. "The Herbaliser" == "Herbaliser")
            alt_artist = re.sub(r"^(?:the|a)\s+", "", norm_artist)
            if norm_query in (norm_artist, alt_artist):
                log.info("Fast-path exact artist match (0ms): '%s' -> mode='artist', target_artist='%s'", query, artist)
                return PlaybackIntent(mode="artist", target_artist=artist)

        return None

    def parse(self, query: str, catalog: dict) -> PlaybackIntent:
        artists = catalog.get("artists", [])
        fast_intent = self._try_fast_artist_match(query, artists)
        if fast_intent is not None:
            return fast_intent

        if not self.api_key or not self._client:
            raise ValueError("GEMINI_API_KEY is required for playback commands")

        # Build compact catalog to minimize input token latency
        compact_catalog = {
            "artists": artists,
            "genres": catalog.get("genres", []),
            "moods": catalog.get("moods", [])[:40],
        }
        # Only include album list if query hints at an album
        if any(w in query.lower() for w in ("album", "record", "lp", "ep")):
            compact_catalog["albums"] = catalog.get("albums", [])

        catalog_json = json.dumps(compact_catalog, separators=(",", ":"))
        contents = (
            "You are an intelligent music selector and DJ for a personal Plex music library.\n"
            "Interpret the listener request and choose exact artist/album names from the catalog.\n\n"
            "MODES:\n"
            "- 'artist': DEFAULT mode whenever the listener names an artist to play "
            "(e.g., 'DJ Shadow', 'play DJ Shadow', 'switch to Fugazi', 'play only Portishead'). "
            "Set target_artist to the exact catalog artist name. seed_artists can be empty.\n"
            "- 'artist_radio': ONLY use when the listener explicitly asks for a radio, station, mix, or music similar to an artist "
            "(e.g., 'play DJ Shadow radio', 'station for Fugazi', 'stream a mix like DJ Shadow', 'music like Massive Attack'). "
            "Set target_artist. CRITICAL: seed_artists MUST include target_artist PLUS 5 to 8 sonically similar peer artists "
            "chosen strictly from the catalog artists list. Never invent artists not in the catalog.\n"
            "- 'album': When a specific album is requested. Set target_album and target_artist if known.\n"
            "- 'track': When an individual song/track is requested. Set target_track and target_artist/target_album if known.\n"
            "- 'theme_mix': For genre, decade, mood, or thematic requests (e.g., 'play 90s trip hop', 'upbeat rock', 'chill electronic'). "
            "Populate matched_genres, seed_artists (5 to 8 relevant artists from catalog), decades, and moods.\n\n"
            f"Catalog: {catalog_json}\n"
            f"Listener request: {query}"
        )

        last_error = None
        for model in CANDIDATE_MODELS:
            try:
                response = self._client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=self._config,
                )
                if response.parsed is not None:
                    return PlaybackIntent.model_validate(response.parsed)
                return PlaybackIntent.model_validate_json(response.text)
            except Exception as exc:
                log.warning("Model %s failed (%s), trying fallback candidate...", model, exc)
                last_error = exc
        raise RuntimeError(f"All Gemini candidate models failed. Last error: {last_error}") from last_error
