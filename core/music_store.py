"""Plugin music storage, independent of playback and web routes."""

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from .model import Song


class MusicStore:
    """Store shared songs, playback history, and playlists in the plugin database.

    Call initialize() before using the store. Each operation closes its own
    connection, so there is no shutdown hook to wire into the plugin. Database
    errors propagate to the caller, which decides whether to log or report them.
    Songs are unique by player, actual source, and source song ID. History
    and playlists reference the same songs independently. Playlists are global,
    identified by ID, and may have duplicate names. Times are UTC Unix timestamps
    in seconds. Recording a play does not impose a retention limit or verify that
    playback actually succeeded.
    """

    def __init__(self, db_path: Path):
        """Accept the configured database location without creating files.

        Args:
            db_path: Database path defined by PluginConfig.db_path.
        """
        self.db_path = Path(db_path)

    @asynccontextmanager
    async def _connect(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Open a connection with foreign keys enabled for related music tables.

        Yields:
            A connection that closes when the operation finishes.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            db.row_factory = aiosqlite.Row
            yield db

    async def initialize(self) -> None:
        """Create the directory, music tables, and indexes if absent."""
        await asyncio.to_thread(self.db_path.parent.mkdir, parents=True, exist_ok=True)
        async with self._connect() as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS songs (
                    id INTEGER PRIMARY KEY,
                    player TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_song_id TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    UNIQUE (player, source, source_song_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS play_history (
                    song_id INTEGER PRIMARY KEY REFERENCES songs(id)
                        ON DELETE CASCADE,
                    first_played_at REAL NOT NULL,
                    last_played_at REAL NOT NULL,
                    play_count INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_play_history_recent
                ON play_history (last_played_at DESC)
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS playlists (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS playlist_songs (
                    playlist_id INTEGER NOT NULL REFERENCES playlists(id)
                        ON DELETE CASCADE,
                    song_id INTEGER NOT NULL REFERENCES songs(id)
                        ON DELETE CASCADE,
                    position INTEGER NOT NULL CHECK (position >= 0),
                    added_at REAL NOT NULL,
                    PRIMARY KEY (playlist_id, song_id)
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_playlist_songs_order
                ON playlist_songs (playlist_id, position, song_id)
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS favorites (
                    song_id INTEGER PRIMARY KEY REFERENCES songs(id)
                        ON DELETE CASCADE,
                    added_at REAL NOT NULL
                )
                """
            )
            await db.commit()

    async def set_favorite(self, player: str, song: Song, favorite: bool) -> int:
        """Set collection state explicitly, making retried requests idempotent.

        Args:
            player: Adapter identifier that owns the song.
            song: Source identity and durable metadata.
            favorite: Whether the song should belong to favorites.

        Returns:
            Stable local song ID. History and custom playlists are unaffected.
        """
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            song_id = await self._save_song(db, player, song)
            if favorite:
                await db.execute(
                    "INSERT OR IGNORE INTO favorites (song_id, added_at) VALUES (?, ?)",
                    (song_id, time.time()),
                )
            else:
                await db.execute("DELETE FROM favorites WHERE song_id = ?", (song_id,))
            await db.commit()
        return song_id

    async def favorite_keys(self) -> set[tuple[str, str, str]]:
        """Read favorite identities in one query for batch API serialization.

        Returns:
            Adapter, actual source, and platform ID tuples.
        """
        async with self._connect() as db:
            async with db.execute(
                "SELECT player, source, source_song_id FROM songs "
                "JOIN favorites ON favorites.song_id = songs.id"
            ) as cursor:
                return {tuple(row) for row in await cursor.fetchall()}

    async def list_favorites(
        self, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """Read a favorites page and its count from the same snapshot.

        Args:
            limit: Positive page size.
            offset: Nonnegative number of entries to skip.

        Returns:
            Entries with song_id, player, song, and added_at, and the total count.

        Raises:
            ValueError: If pagination is invalid.
        """
        if limit <= 0 or offset < 0:
            raise ValueError("limit must be positive and offset must be nonnegative")
        async with self._connect() as db:
            await db.execute("BEGIN")
            async with db.execute("SELECT COUNT(*) FROM favorites") as cursor:
                total = (await cursor.fetchone())[0]
            async with db.execute(
                "SELECT songs.*, favorites.added_at FROM favorites "
                "JOIN songs ON songs.id = favorites.song_id "
                "ORDER BY favorites.added_at DESC, songs.id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "song_id": row["id"],
                "player": row["player"],
                "song": Song(
                    id=row["source_song_id"],
                    source=row["source"],
                    **json.loads(row["metadata_json"]),
                ),
                "added_at": row["added_at"],
            }
            for row in rows
        ], total

    async def _save_song(
        self, db: aiosqlite.Connection, player: str, song: Song
    ) -> int:
        """Upsert metadata within a caller-owned immediate transaction.

        Args:
            db: Connection with an active write transaction; never committed here.
            player: Adapter identifier, such as txqq or qqmusic.
            song: Song with its actual source and replay metadata.
                Missing metadata preserves previously stored values. Audio URLs,
                local paths, lyrics, and comments are not persisted.

        Returns:
            Stable local song ID, distinct from Song.id in the source platform.

        Raises:
            ValueError: If the player or song ID is empty.
        """
        player = player.strip().lower()
        song_id = str(song.id).strip() if song.id is not None else ""
        if not player or not song_id:
            raise ValueError("player and song ID must not be empty")
        source = (song.source or "").strip().lower() or player
        metadata = {
            name: getattr(song, name)
            for name in (
                "name",
                "artists",
                "duration",
                "cover_url",
                "page_url",
                "card_type",
                "note",
                "album_id",
                "album_audio_id",
            )
            if getattr(song, name) is not None
        }
        async with db.execute(
            """
            SELECT metadata_json FROM songs
            WHERE player = ? AND source = ? AND source_song_id = ?
            """,
            (player, source, song_id),
        ) as cursor:
            previous = await cursor.fetchone()
        if previous is not None:
            metadata = json.loads(previous[0]) | metadata
        await db.execute(
            """
            INSERT INTO songs (player, source, source_song_id, metadata_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (player, source, source_song_id) DO UPDATE SET
                metadata_json = excluded.metadata_json
            """,
            (player, source, song_id, json.dumps(metadata, ensure_ascii=False)),
        )
        async with db.execute(
            """
            SELECT id FROM songs
            WHERE player = ? AND source = ? AND source_song_id = ?
            """,
            (player, source, song_id),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return row[0]

    async def save_song(self, player: str, song: Song) -> int:
        """Save shared song metadata without recording a play.

        Args:
            player: Adapter identifier, lowercased with surrounding whitespace removed.
            song: Source identity and metadata, following record_play's rules.

        Returns:
            Stable local song ID used by playlist entries.

        Raises:
            ValueError: If the player or song ID is empty.
        """
        async with self._connect() as db:
            # Lock before reading so concurrent writers cannot lose metadata.
            await db.execute("BEGIN IMMEDIATE")
            song_id = await self._save_song(db, player, song)
            await db.commit()
        return song_id

    async def record_play(self, player: str, song: Song) -> int:
        """Save metadata and history atomically after a successful play.

        Args:
            player: Adapter identifier, such as txqq or qqmusic.
            song: Played song with replay metadata. Missing values preserve prior
                metadata; audio URLs, local paths, lyrics, and comments are omitted.

        Returns:
            Local song ID of the recorded entry.

        Raises:
            ValueError: If the player or song ID is empty.
        """
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            song_id = await self._save_song(db, player, song)
            played_at = time.time()
            await db.execute(
                """
                INSERT INTO play_history (
                    song_id, first_played_at, last_played_at, play_count
                )
                VALUES (?, ?, ?, 1)
                ON CONFLICT (song_id) DO UPDATE SET
                    last_played_at = excluded.last_played_at,
                    play_count = play_history.play_count + 1
                """,
                (song_id, played_at, played_at),
            )
            await db.commit()
        return song_id

    async def get_song(self, song_id: int) -> dict[str, Any] | None:
        """Read shared metadata for playback or collection by local identity.

        Args:
            song_id: Local song ID, not the platform's Song.id.

        Returns:
            A dictionary with song_id, player, and Song under song, or None.
        """
        async with self._connect() as db:
            async with db.execute(
                "SELECT * FROM songs WHERE id = ?", (song_id,)
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "song_id": row["id"],
            "player": row["player"],
            "song": Song(
                id=row["source_song_id"],
                source=row["source"],
                **json.loads(row["metadata_json"]),
            ),
        }

    async def list_history(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        player: str | None = None,
        source: str | None = None,
        random_order: bool = False,
    ) -> list[dict[str, Any]]:
        """Read history in order of most recent playback.

        Args:
            limit: Maximum number of entries to return, greater than zero.
            offset: Number of entries to skip, zero or greater.
            player: Optional adapter identifier to filter by.
            source: Optional actual source identifier to filter by.
            random_order: Shuffle matching entries instead of sorting by recency.

        Returns:
            Entries containing the local song_id, player, a reconstructed Song
            under song, first_played_at, last_played_at, and play_count. Ties are
            ordered by player, source, and song ID for stable pagination.

        Raises:
            ValueError: If the limit or offset is invalid.
        """
        if limit <= 0 or offset < 0:
            raise ValueError("limit must be positive and offset must be nonnegative")
        player = player.strip().lower() if player is not None else None
        source = source.strip().lower() if source is not None else None
        order = (
            "RANDOM()"
            if random_order
            else "last_played_at DESC, player, source, source_song_id"
        )
        async with self._connect() as db:
            async with db.execute(
                f"""
                SELECT songs.*, play_history.first_played_at,
                    play_history.last_played_at, play_history.play_count
                FROM play_history JOIN songs ON songs.id = play_history.song_id
                WHERE (? IS NULL OR player = ?) AND (? IS NULL OR source = ?)
                ORDER BY {order}
                LIMIT ? OFFSET ?
                """,
                (player, player, source, source, limit, offset),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "song_id": row["id"],
                "player": row["player"],
                "song": Song(
                    id=row["source_song_id"],
                    source=row["source"],
                    **json.loads(row["metadata_json"]),
                ),
                "first_played_at": row["first_played_at"],
                "last_played_at": row["last_played_at"],
                "play_count": row["play_count"],
            }
            for row in rows
        ]

    async def count_history(
        self, *, player: str | None = None, source: str | None = None
    ) -> int:
        """Count distinct history entries matching optional source filters.

        Args:
            player: Optional adapter identifier to filter by.
            source: Optional actual source identifier to filter by.

        Returns:
            Number of matching songs, rather than number of plays.
        """
        player = player.strip().lower() if player is not None else None
        source = source.strip().lower() if source is not None else None
        async with self._connect() as db:
            async with db.execute(
                """
                SELECT COUNT(*)
                FROM play_history JOIN songs ON songs.id = play_history.song_id
                WHERE (? IS NULL OR player = ?) AND (? IS NULL OR source = ?)
                """,
                (player, player, source, source),
            ) as cursor:
                row = await cursor.fetchone()
        return row[0]  # type: ignore

    async def delete_history(
        self, player: str, song_id: str, *, source: str | None = None
    ) -> bool:
        """Delete exactly one song's history while retaining shared song metadata.

        Args:
            player: Adapter identifier of the history entry.
            song_id: Song identifier within the actual source.
            source: Actual source identifier; defaults to player.

        Returns:
            Whether a matching entry was deleted.
        """
        player = player.strip().lower()
        source = (source or "").strip().lower() or player
        async with self._connect() as db:
            async with db.execute(
                """
                DELETE FROM play_history
                WHERE song_id IN (
                    SELECT id FROM songs
                    WHERE player = ? AND source = ? AND source_song_id = ?
                )
                """,
                (player, source, str(song_id).strip()),
            ) as cursor:
                deleted = cursor.rowcount > 0
            await db.commit()
        return deleted

    async def clear_history(self) -> int:
        """Remove all playback history while retaining shared song metadata.

        Returns:
            Number of distinct history entries deleted.
        """
        async with self._connect() as db:
            async with db.execute("DELETE FROM play_history") as cursor:
                deleted = cursor.rowcount
            await db.commit()
        return deleted

    async def create_playlist(self, name: str) -> int:
        """Create an empty playlist; names need not be unique.

        Args:
            name: Display name, stripped of leading and trailing whitespace.

        Returns:
            Local playlist ID used for subsequent operations.

        Raises:
            ValueError: If the name is empty after stripping whitespace.
        """
        name = name.strip()
        if not name:
            raise ValueError("playlist name must not be empty")
        now = time.time()
        async with self._connect() as db:
            async with db.execute(
                "INSERT INTO playlists (name, created_at, updated_at) VALUES (?, ?, ?)",
                (name, now, now),
            ) as cursor:
                playlist_id = cursor.lastrowid
            await db.commit()
        assert playlist_id is not None
        return playlist_id

    async def list_playlists(
        self, *, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """List playlists by latest modification, with stable ID tie ordering.

        Args:
            limit: Maximum number of playlists, greater than zero.
            offset: Number of playlists to skip, zero or greater.

        Returns:
            Dictionaries with id, name, created_at, updated_at, and song_count.

        Raises:
            ValueError: If the limit or offset is invalid.
        """
        if limit <= 0 or offset < 0:
            raise ValueError("limit must be positive and offset must be nonnegative")
        async with self._connect() as db:
            async with db.execute(
                """
                SELECT playlists.*, COUNT(playlist_songs.song_id) AS song_count
                FROM playlists LEFT JOIN playlist_songs
                    ON playlists.id = playlist_songs.playlist_id
                GROUP BY playlists.id
                ORDER BY updated_at DESC, playlists.id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def get_playlist(self, playlist_id: int) -> dict[str, Any] | None:
        """Read one playlist's metadata and size.

        Args:
            playlist_id: Local playlist ID.

        Returns:
            Dictionary with id, name, created_at, updated_at, and song_count,
            or None if the playlist does not exist.
        """
        async with self._connect() as db:
            async with db.execute(
                """
                SELECT playlists.*, COUNT(playlist_songs.song_id) AS song_count
                FROM playlists LEFT JOIN playlist_songs
                    ON playlists.id = playlist_songs.playlist_id
                WHERE playlists.id = ?
                GROUP BY playlists.id
                """,
                (playlist_id,),
            ) as cursor:
                row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def count_playlists(self) -> int:
        """Count playlists for paginated catalog responses.

        Returns:
            Total number of playlists, including empty playlists.
        """
        async with self._connect() as db:
            async with db.execute("SELECT COUNT(*) FROM playlists") as cursor:
                row = await cursor.fetchone()
        assert row is not None
        return row[0]

    async def rename_playlist(self, playlist_id: int, name: str) -> bool:
        """Change a playlist's name and modification time.

        Args:
            playlist_id: Local playlist ID.
            name: New display name, stripped of surrounding whitespace.

        Returns:
            Whether the playlist exists and was updated.

        Raises:
            ValueError: If the name is empty after stripping whitespace.
        """
        name = name.strip()
        if not name:
            raise ValueError("playlist name must not be empty")
        async with self._connect() as db:
            async with db.execute(
                "UPDATE playlists SET name = ?, updated_at = ? WHERE id = ?",
                (name, time.time(), playlist_id),
            ) as cursor:
                updated = cursor.rowcount > 0
            await db.commit()
        return updated

    async def delete_playlist(self, playlist_id: int) -> bool:
        """Delete a playlist and its memberships, retaining songs and history.

        Args:
            playlist_id: Local playlist ID.

        Returns:
            Whether a matching playlist was deleted.
        """
        async with self._connect() as db:
            async with db.execute(
                "DELETE FROM playlists WHERE id = ?", (playlist_id,)
            ) as cursor:
                deleted = cursor.rowcount > 0
            await db.commit()
        return deleted

    async def add_playlist_song(
        self, playlist_id: int, player: str, song: Song
    ) -> bool:
        """Save metadata and append a song atomically without recording a play.

        Args:
            playlist_id: Local playlist ID.
            player: Adapter identifier, such as txqq or qqmusic.
            song: Song to save, following record_play's metadata rules.

        Returns:
            True for a new membership. An existing membership returns False and
            keeps its order and added_at, but still updates shared song metadata.

        Raises:
            LookupError: If the playlist does not exist.
            ValueError: If the player or song ID is empty.
        """
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT id FROM playlists WHERE id = ?", (playlist_id,)
            ) as cursor:
                if await cursor.fetchone() is None:
                    raise LookupError(f"playlist {playlist_id} does not exist")
            song_id = await self._save_song(db, player, song)
            now = time.time()
            async with db.execute(
                """
                INSERT INTO playlist_songs (playlist_id, song_id, position, added_at)
                SELECT ?, ?, COALESCE(MAX(position), -1) + 1, ?
                FROM playlist_songs WHERE playlist_id = ?
                ON CONFLICT (playlist_id, song_id) DO NOTHING
                """,
                (playlist_id, song_id, now, playlist_id),
            ) as cursor:
                added = cursor.rowcount > 0
            if added:
                await db.execute(
                    "UPDATE playlists SET updated_at = ? WHERE id = ?",
                    (now, playlist_id),
                )
            await db.commit()
        return added

    async def list_playlist_songs(
        self, playlist_id: int, *, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Read a playlist in its saved order with stable pagination.

        Args:
            playlist_id: Local playlist ID.
            limit: Maximum number of entries, greater than zero.
            offset: Number of entries to skip, zero or greater.

        Returns:
            Entries with local song_id, player, reconstructed Song under song,
            position, and added_at. Positions are sorting keys, not row numbers,
            and may have gaps after removals.

        Raises:
            LookupError: If the playlist does not exist.
            ValueError: If the limit or offset is invalid.
        """
        if limit <= 0 or offset < 0:
            raise ValueError("limit must be positive and offset must be nonnegative")
        async with self._connect() as db:
            # Keep the existence check and page query in the same read snapshot.
            await db.execute("BEGIN")
            async with db.execute(
                "SELECT id FROM playlists WHERE id = ?", (playlist_id,)
            ) as cursor:
                if await cursor.fetchone() is None:
                    raise LookupError(f"playlist {playlist_id} does not exist")
            async with db.execute(
                """
                SELECT songs.*, playlist_songs.position, playlist_songs.added_at
                FROM playlist_songs JOIN songs ON songs.id = playlist_songs.song_id
                WHERE playlist_id = ?
                ORDER BY position, songs.id
                LIMIT ? OFFSET ?
                """,
                (playlist_id, limit, offset),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "song_id": row["id"],
                "player": row["player"],
                "song": Song(
                    id=row["source_song_id"],
                    source=row["source"],
                    **json.loads(row["metadata_json"]),
                ),
                "position": row["position"],
                "added_at": row["added_at"],
            }
            for row in rows
        ]

    async def remove_playlist_song(self, playlist_id: int, song_id: int) -> bool:
        """Remove a membership, retaining the song and other references.

        Args:
            playlist_id: Local playlist ID.
            song_id: Local song ID returned by save_song or list queries, not Song.id.

        Returns:
            Whether a matching membership was deleted. Missing playlists also
            return False. Remaining positions are unchanged.
        """
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "DELETE FROM playlist_songs WHERE playlist_id = ? AND song_id = ?",
                (playlist_id, song_id),
            ) as cursor:
                deleted = cursor.rowcount > 0
            if deleted:
                await db.execute(
                    "UPDATE playlists SET updated_at = ? WHERE id = ?",
                    (time.time(), playlist_id),
                )
            await db.commit()
        return deleted

    async def reorder_playlist_songs(
        self, playlist_id: int, song_ids: list[int]
    ) -> None:
        """Replace the complete song order atomically without changing membership.

        Args:
            playlist_id: Local playlist ID.
            song_ids: All current local song IDs, once each, in the desired order.
                Pass an empty list only for an empty playlist.

        Raises:
            LookupError: If the playlist does not exist.
            ValueError: If IDs repeat or do not exactly match current membership.
                A stale list after concurrent membership changes is also rejected.
        """
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT id FROM playlists WHERE id = ?", (playlist_id,)
            ) as cursor:
                if await cursor.fetchone() is None:
                    raise LookupError(f"playlist {playlist_id} does not exist")
            async with db.execute(
                "SELECT song_id FROM playlist_songs WHERE playlist_id = ?",
                (playlist_id,),
            ) as cursor:
                current_ids = {row[0] for row in await cursor.fetchall()}
            if len(song_ids) != len(current_ids) or set(song_ids) != current_ids:
                raise ValueError("song IDs must match every playlist song exactly once")
            await db.executemany(
                """
                UPDATE playlist_songs SET position = ?
                WHERE playlist_id = ? AND song_id = ?
                """,
                [
                    (position, playlist_id, song_id)
                    for position, song_id in enumerate(song_ids)
                ],
            )
            await db.execute(
                "UPDATE playlists SET updated_at = ? WHERE id = ?",
                (time.time(), playlist_id),
            )
            await db.commit()
