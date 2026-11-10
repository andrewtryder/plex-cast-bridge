# Optional local music index

The music index is disabled by default. Existing Smart DJ playback does not depend on it. In Settings → Library, enable indexing and choose manual, daily, or weekly scans. Indexing reads Plex metadata in pages of 100 and stores the resulting snapshot in `DATA_DIR/music_index.sqlite3` (normally `/data/music_index.sqlite3` in Docker). Scans do not play music or change Plex playlists.

An initial scan reads the library's track inventory. Later scans still enumerate that inventory to detect removals, but only update changed track records. An active index remains available while a replacement scan is built; cancellation, failure, or an interrupted restart leaves the last completed snapshot in place. Prevent overlapping scans and preserve the `data/` directory across container replacements. The index may reveal listening-library metadata; protect and back it up accordingly.

The status page reports indexed track, artist, and album counts and the availability of artist/album names, years, genre tags, and mood tags. Missing tags remain missing. Different Plex server and library identifiers are kept separate. No Plex tokens, audio files, or server addresses are stored in the index.

AI-assisted generation and mix management are not yet available in this foundation. No track metadata is sent to Gemini by indexing. The database has reserved tables for bridge-managed mixes, but this version does not create or alter Plex playlists.
