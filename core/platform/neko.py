from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp

from astrbot.api import logger

from ..config import PluginConfig
from ..model import Platform, Playlist, Song
from .base import BaseMusicPlayer


class NekoMusic(BaseMusicPlayer):
    """Neko Music API adapter for search and media URL resolution."""

    platform: ClassVar[Platform] = Platform(
        name="neko",
        display_name="Neko云音乐",
    )
    supported_audio_qualities: ClassVar[tuple[str, ...]] = (
        "standard",
        "hq",
        "sq",
        "hires",
    )
    supports_playlist_import = True
    DEFAULT_BASE_URL = "https://music.nekocore.cn"
    CLIENT_NAME = "NekoMusic-AstrBot/1.0"
    HEADERS: ClassVar[dict[str, str]] = {
        "User-Agent": CLIENT_NAME,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    def __init__(self, config: PluginConfig):
        """Configure the adapter for one NekoMusic server.

        Args:
            config: Plugin configuration containing the NekoMusic base URL and proxy.
        """
        super().__init__(config)
        self.base_url = (self.platform_cfg.base_url or self.DEFAULT_BASE_URL).rstrip(
            "/"
        )

    async def _request_api(self, method: str, endpoint: str, **kwargs):
        """Send one request to a public NekoMusic API endpoint.

        Args:
            method: HTTP method used for the request.
            endpoint: Absolute API path below the configured NekoMusic origin.
            **kwargs: Additional arguments forwarded to ``aiohttp``.

        Returns:
            Decoded JSON data, or None when the request fails.
        """
        session = self._get_session()
        proxy = self.platform_cfg.proxy or None
        headers = dict(self.HEADERS)
        headers.update(kwargs.pop("headers", {}) or {})
        try:
            async with session.request(
                method,
                f"{self.base_url}{endpoint}",
                headers=headers,
                proxy=proxy,
                **kwargs,
            ) as response:
                if response.status != 200:
                    body = (await response.text())[:200]
                    logger.warning(
                        f"Neko API {method} {endpoint} returned HTTP "
                        f"{response.status}: {body}"
                    )
                    return None
                try:
                    return await response.json(content_type=None)
                except ValueError as exc:
                    logger.warning(f"Neko API returned invalid JSON: {exc}")
                    return None
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            logger.warning(f"Neko API {method} {endpoint} failed: {exc}")
            return None

    async def fetch_songs(
        self,
        keyword: str,
        limit: int,
        extra: str | None = None,
    ) -> list[Song]:
        """Search songs with the Neko Music API.

        Args:
            keyword: Song or artist search text.
            limit: Maximum number of results to return.
            extra: Unused platform-specific command context.

        Returns:
            Matching Neko Music songs.
        """
        del extra
        result = await self._request_api(
            "POST",
            "/api/music/search",
            json={"query": keyword},
        )
        songs_data = result.get("results") if isinstance(result, Mapping) else None
        if not isinstance(result, Mapping) or not result.get("success"):
            logger.warning("Neko Music search returned an unsuccessful response")
            return []
        if not isinstance(songs_data, list):
            logger.warning("Neko Music search returned an invalid result list")
            return []

        songs = []
        for item in songs_data[:limit]:
            if not isinstance(item, Mapping) or item.get("id") is None:
                continue
            song_id = str(item["id"])
            artists = (
                item.get("artist")
                or item.get("singer")
                or item.get("ar")
                or item.get("author")
            )
            if isinstance(artists, list):
                artists = "、".join(
                    artist.get("name", "")
                    if isinstance(artist, Mapping)
                    else str(artist)
                    for artist in artists
                )
            elif isinstance(artists, Mapping):
                artists = artists.get("name")
            album = item.get("album", item.get("al"))
            if isinstance(album, Mapping):
                album = album.get("name")
            if not isinstance(album, str):
                album = None
            duration = item.get("duration")
            if (
                isinstance(duration, (int, float))
                and not isinstance(duration, bool)
                and duration > 0
            ):
                duration = int(duration * 1000)
            songs.append(
                Song(
                    id=song_id,
                    source=self.platform.name,
                    card_type="qq",
                    name=item.get("name", item.get("title")),
                    artists=str(artists) if artists else None,
                    duration=int(duration)
                    if isinstance(duration, (int, float))
                    else None,
                    cover_url=f"{self.base_url}/api/music/cover/{song_id}",
                    page_url=f"{self.base_url}/detail/{song_id}",
                    note=album,
                )
            )
        return songs

    async def fetch_playlist(self, reference: str, limit: int = 1000) -> Playlist:
        """Fetch a public NekoMusic playlist and normalize its tracks.

        Args:
            reference: Numeric playlist ID or a playlist URL on this NekoMusic host.
            limit: Maximum number of tracks to return.

        Returns:
            Public playlist metadata and normalized NekoMusic songs.

        Raises:
            ValueError: If the reference or limit is invalid.
            RuntimeError: If playlist metadata or tracks cannot be loaded.
        """
        value = reference.strip()
        playlist_id = value if value.isdigit() else ""
        if not playlist_id:
            parsed = urlparse(value)
            configured = urlparse(self.base_url)
            allowed_hosts = {
                (urlparse(self.DEFAULT_BASE_URL).hostname or "").lower(),
                (configured.hostname or "").lower(),
            }
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.hostname.lower() not in allowed_hosts
            ):
                raise ValueError("invalid NekoMusic playlist URL")
            playlist_id = parse_qs(parsed.query).get("id", [""])[0]
            if not playlist_id:
                parts = [part for part in parsed.path.split("/") if part]
                try:
                    marker = parts.index("playlist")
                    playlist_id = parts[marker + 1]
                except (ValueError, IndexError):
                    playlist_id = ""
        if not playlist_id.isdigit() or int(playlist_id) <= 0:
            raise ValueError("NekoMusic playlist ID must be a positive integer")
        if not 1 <= limit <= 1000:
            raise ValueError("playlist import limit must be between 1 and 1000")

        detail = await self._request_api("GET", f"/api/playlist/{playlist_id}")
        metadata = detail.get("playlist") if isinstance(detail, Mapping) else None
        if (
            not isinstance(detail, Mapping)
            or not detail.get("success")
            or not isinstance(metadata, Mapping)
            or not metadata.get("name")
        ):
            raise RuntimeError("NekoMusic could not load playlist details")
        result = await self._request_api(
            "GET", f"/api/user/playlist/music/{playlist_id}"
        )
        items = result.get("musicList") if isinstance(result, Mapping) else None
        if (
            not isinstance(result, Mapping)
            or not result.get("success")
            or not isinstance(items, list)
        ):
            raise RuntimeError("NekoMusic could not load playlist tracks")

        songs: list[Song] = []
        for item in items[:limit]:
            if not isinstance(item, Mapping) or item.get("id") is None:
                continue
            song_id = str(item["id"])
            duration = item.get("duration")
            duration_ms = (
                int(duration * 1000)
                if isinstance(duration, (int, float))
                and not isinstance(duration, bool)
                and duration > 0
                else None
            )
            cover = item.get("coverUrl") or item.get("coverPath")
            songs.append(
                Song(
                    id=song_id,
                    source=self.platform.name,
                    card_type="qq",
                    name=item.get("title") or item.get("name"),
                    artists=item.get("artist"),
                    duration=duration_ms,
                    cover_url=(
                        urljoin(f"{self.base_url}/", cover)
                        if isinstance(cover, str) and cover
                        else f"{self.base_url}/api/music/cover/{song_id}"
                    ),
                    page_url=f"{self.base_url}/detail/{song_id}",
                    note=item.get("album"),
                )
            )

        creator = metadata.get("creator")
        return Playlist(
            id=playlist_id,
            source=self.platform.name,
            name=str(metadata["name"]),
            songs=songs,
            cover_url=songs[0].cover_url if songs else None,
            creator=(
                str(creator["nickname"])
                if isinstance(creator, Mapping) and creator.get("nickname")
                else None
            ),
            description=(
                str(metadata["description"])
                if metadata.get("description") is not None
                else None
            ),
        )

    async def fetch_extra(self, song: Song) -> Song:
        """Resolve the Neko API media pointer to a downloadable URL.

        Args:
            song: Song returned by the Neko search API.

        Returns:
            The song with its direct audio URL when available.
        """
        if song.audio_url:
            return song
        result = await self._request_api("GET", f"/api/music/file/{song.id}")
        data = result.get("data") if isinstance(result, Mapping) else None
        media_url = data.get("url") if isinstance(data, Mapping) else None
        if (
            isinstance(result, Mapping)
            and isinstance(data, Mapping)
            and result.get("success")
            and isinstance(media_url, str)
        ):
            song.audio_url = urljoin(f"{self.base_url}/", media_url)
            quality = data.get("quality")
            if isinstance(quality, (str, int, float)) and not isinstance(quality, bool):
                song.quality = str(quality)
        else:
            logger.warning(f"Neko Music did not return an audio URL for song {song.id}")
        return song

    async def download_cover(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> bytes | None:
        """Download a Neko cover using the platform proxy and client headers.

        Args:
            url: Cover image URL.
            headers: Optional header override.

        Returns:
            Image bytes, or None if the request fails.
        """
        return await super().download_cover(url, headers=headers or self.HEADERS)

    async def download_audio(
        self, song: Song, quality: str | None = None
    ) -> Path | None:
        """Resolve and download the requested NekoMusic audio quality.

        Args:
            song: NekoMusic song to download.
            quality: One of standard, hq, sq, or hires.

        Returns:
            Path to the downloaded audio, or None if the request fails.
        """
        requested_quality = quality or "hq"
        if requested_quality not in self.supported_audio_qualities:
            logger.warning(
                f"Unsupported Neko Music quality {requested_quality}; using hq"
            )
            requested_quality = "hq"
        result = await self._request_api(
            "GET",
            f"/api/music/file/{song.id}",
            params={"quality": requested_quality},
        )
        data = result.get("data") if isinstance(result, Mapping) else None
        media_url = data.get("url") if isinstance(data, Mapping) else None
        if (
            isinstance(result, Mapping)
            and isinstance(data, Mapping)
            and result.get("success")
            and isinstance(media_url, str)
            and media_url
        ):
            song.audio_url = urljoin(f"{self.base_url}/", media_url)
            quality_value = data.get("quality")
            song.quality = (
                str(quality_value)
                if isinstance(quality_value, (str, int, float))
                and not isinstance(quality_value, bool)
                else requested_quality
            )
        if not song.audio_url:
            return None
        return await self._download_to_temp(
            song.audio_url,
            headers=self.HEADERS,
            allowed_suffixes=frozenset(
                {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav"}
            ),
        )

    async def fetch_comments(self, song: Song) -> Song:
        """Load the first page of public NekoMusic comments.

        Args:
            song: Song returned by the Neko search API.

        Returns:
            The song with public top-level comments when available.
        """
        if song.comments:
            return song
        result = await self._request_api(
            "GET",
            "/api/comments",
            params={"musicId": song.id, "page": 1, "pageSize": 50},
        )
        data = result.get("data") if isinstance(result, Mapping) else None
        comments = data.get("comments") if isinstance(data, Mapping) else None
        if (
            isinstance(result, Mapping)
            and result.get("success")
            and isinstance(comments, list)
            and comments
        ):
            song.comments = comments
        return song

    async def fetch_lyrics(self, song: Song) -> Song:
        """Load stored LRC lyrics from the NekoMusic song detail endpoint.

        Args:
            song: Song returned by the Neko search API.

        Returns:
            The song with lyrics when NekoMusic has non-placeholder LRC content.
        """
        if song.lyrics:
            return song
        result = await self._request_api("GET", f"/api/music/info/{song.id}")
        data = result.get("data") if isinstance(result, Mapping) else None
        lyrics = data.get("lyrics") if isinstance(data, Mapping) else None
        if (
            isinstance(result, Mapping)
            and result.get("success")
            and isinstance(lyrics, str)
            and lyrics.strip()
            and "Neko歌姬计划 暂无歌词" not in lyrics
        ):
            song.lyrics = lyrics
        return song
