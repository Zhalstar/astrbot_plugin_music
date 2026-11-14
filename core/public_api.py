import asyncio
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .model import Song
from .music_service import MusicService


class MusicApi:
    """Expose a versioned, transport-neutral API to companion plugins."""

    version = 1

    def __init__(self, music: MusicService):
        self.music = music

    def sources(self) -> list[dict[str, str]]:
        """Return enabled sources in configured priority order.

        Returns:
            Source identifiers and display names safe for external presentation.
        """
        return [
            {
                "name": player.platform.name,
                "display_name": player.platform.display_name,
            }
            for player in self.music.get_players_by_priority()
        ]

    def has_source(self, name: str) -> bool:
        """Check whether a source is currently enabled.

        Args:
            name: Source identifier or display name.

        Returns:
            Whether the source can currently serve requests.
        """
        return self.music.get_player(name) is not None

    async def search(
        self,
        query: str,
        *,
        source: str = "",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search music and return serializable source-owned track payloads.

        Args:
            query: Song title, artist, or other source search text.
            source: Exclusive source identifier, or empty to use priority fallback.
            limit: Maximum number of results requested from the selected source.

        Returns:
            Search results containing a source identifier and opaque song payload.

        Raises:
            ValueError: If the requested source is unavailable.
        """
        result = await self.music.search_songs(
            query,
            platform=source,
            limit=limit,
            fallback=not source,
            require_match=False,
        )
        if result.player is None:
            return []
        return [
            {
                "source": result.player.platform.name,
                "song": asdict(song),
            }
            for song in result.songs[:limit]
        ]

    async def fetch_playlist(
        self,
        reference: str,
        *,
        source: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Fetch a playlist and return source-owned serializable songs.

        Args:
            reference: Playlist share URL or platform playlist ID.
            source: Source identifier for ambiguous bare IDs, or empty to detect it.
            limit: Maximum number of songs to return.

        Returns:
            Playlist metadata and replayable source-owned song payloads.

        Raises:
            ValueError: If the playlist source cannot be detected or is unavailable.
            NotImplementedError: If the selected source cannot import playlists.
            RuntimeError: If the selected source fails to fetch the playlist.
        """
        player, playlist = await self.music.fetch_playlist(
            reference,
            platform=source,
            limit=limit,
        )
        return {
            "id": playlist.id,
            "name": playlist.name,
            "creator": playlist.creator,
            "cover_url": playlist.cover_url,
            "source": player.platform.name,
            "songs": [
                {
                    "source": player.platform.name,
                    "song": asdict(song),
                }
                for song in playlist.songs[:limit]
            ],
        }

    async def download_audio(
        self,
        source: str,
        song_payload: dict[str, Any],
        quality: str = "standard",
        resolve_timeout: float = 30,
        download_timeout: float = 90,
    ) -> Path | None:
        """Resolve and download one source-owned track.

        Args:
            source: Source identifier returned by :meth:`search`.
            song_payload: Unmodified song payload returned by :meth:`search`.
            quality: Source-specific requested quality.
            resolve_timeout: Maximum seconds allowed for metadata resolution.
            download_timeout: Maximum seconds allowed for the file download.

        Returns:
            Temporary audio path, or ``None`` when downloading fails.

        Raises:
            ValueError: If the source or song payload is unavailable.
        """
        player = self.music.get_player(source)
        if player is None:
            raise ValueError("The requested music source is unavailable")
        song = await asyncio.wait_for(
            self.music.resolve_song(player, Song(**song_payload)), resolve_timeout
        )
        return await asyncio.wait_for(
            player.download_audio(song, quality), download_timeout
        )

    async def track_details(
        self,
        source: str,
        song_payload: dict[str, Any],
        artwork_timeout: float = 10,
        lyrics_timeout: float = 20,
    ) -> dict[str, Any]:
        """Load optional artwork and lyrics for a searched track.

        Args:
            source: Source identifier returned by :meth:`search`.
            song_payload: Unmodified song payload returned by :meth:`search`.
            artwork_timeout: Maximum seconds allowed for artwork metadata.
            lyrics_timeout: Maximum seconds allowed for each lyrics operation.

        Returns:
            Updated serializable song metadata.

        Raises:
            ValueError: If the source is unavailable.
        """
        player = self.music.get_player(source)
        if player is None:
            raise ValueError("The requested music source is unavailable")
        song = replace(Song(**song_payload))
        if not song.cover_url:
            try:
                song = await asyncio.wait_for(player.fetch_extra(song), artwork_timeout)
            except Exception as exc:
                logger.warning(
                    f"Track artwork lookup failed via {source}: {type(exc).__name__}"
                )
        if not song.lyrics:
            song = await asyncio.wait_for(player.fetch_lyrics(song), lyrics_timeout)
        if song.lyrics:
            song = await asyncio.wait_for(player.resolve_lyrics(song), lyrics_timeout)
        return asdict(song)

    def release_audio(self, path: Path) -> None:
        """Delete a temporary audio file created by :meth:`download_audio`.

        Args:
            path: Path returned by :meth:`download_audio`.
        """
        path = Path(path)
        if path.parent.resolve() == self.music.cfg.temp_dir.resolve():
            path.unlink(missing_ok=True)
