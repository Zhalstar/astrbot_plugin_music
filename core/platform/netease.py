from typing import ClassVar
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from astrbot.api import logger

from ..config import PluginConfig
from ..model import Platform, Song
from .base import BaseMusicPlayer


class NetEaseMusic(BaseMusicPlayer):
    """NetEase Cloud Music source backed by Qijie's Meting API."""

    platform: ClassVar[Platform] = Platform(
        name="netease",
        display_name="网易云音乐",
    )
    API_URL = "https://api.qijieya.cn/meting/"

    def __init__(self, config: PluginConfig):
        super().__init__(config)

    def _build_api_url(self, **params: str | int) -> str:
        """Build a Meting URL with correctly encoded query parameters.

        Args:
            **params: Meting query parameters.

        Returns:
            A fully encoded Meting API URL.
        """
        return f"{self.API_URL}?{urlencode(params)}"

    async def _resolve_cover_url(self, cover_url: str) -> str:
        """Resolve a Meting cover endpoint to its final image URL.

        Args:
            cover_url: Meting cover endpoint URL.

        Returns:
            The final URL after redirects, or the original URL on failure.
        """
        try:
            async with self._get_session().get(
                cover_url,
                allow_redirects=True,
                proxy=self.platform_cfg.proxy or None,
            ) as response:
                response.raise_for_status()
                return str(response.url)
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.warning(
                f"NetEase cover redirect failed ({type(exc).__name__}); "
                "using the original URL"
            )
            return cover_url

    async def fetch_songs(self, keyword: str, limit=5, extra=None) -> list[Song]:
        result = await self._request(
            self._build_api_url(
                server="netease", type="search", id=keyword, limit=limit
            )
        )
        if not isinstance(result, list):
            logger.error(f"Unexpected NetEase search response: {result}")
            return []

        songs = []
        for item in result[:limit]:
            if not isinstance(item, dict):
                continue
            song_id = item.get("id")
            if not song_id and isinstance(item.get("url"), str):
                song_id = parse_qs(urlparse(item["url"]).query).get("id", [None])[0]
            if not song_id:
                continue

            artists = item.get("artist") or ""
            if isinstance(artists, list):
                artists = "、".join(
                    artist.get("name", "") if isinstance(artist, dict) else str(artist)
                    for artist in artists
                )
            songs.append(
                Song(
                    id=str(song_id),
                    source=self.platform.name,
                    card_type="163",
                    name=item.get("name"),
                    artists=str(artists),
                    cover_url=item.get("pic")
                    if isinstance(item.get("pic"), str)
                    else None,
                    page_url=f"https://music.163.com/song?id={song_id}",
                    lyrics=item.get("lrc")
                    if isinstance(item.get("lrc"), str)
                    else None,
                )
            )
        return songs

    async def fetch_extra(self, song: Song) -> Song:
        """Fetch playback, cover, and lyric links from the Meting song endpoint.

        Args:
            song: NetEase song whose metadata should be completed.

        Returns:
            The song with any available metadata populated.
        """
        result = await self._request(
            self._build_api_url(server="netease", type="song", id=song.id)
        )
        if isinstance(result, list):
            result = next((item for item in result if isinstance(item, dict)), None)
        if isinstance(result, dict):
            response_id = result.get("id")
            if not response_id and isinstance(result.get("url"), str):
                response_id = parse_qs(urlparse(result["url"]).query).get("id", [None])[
                    0
                ]
            if response_id and str(response_id) != str(song.id):
                logger.warning(
                    f"Meting returned metadata for unexpected song ID: {response_id}"
                )
                result = None
        if isinstance(result, dict):
            if not song.audio_url and isinstance(result.get("url"), str):
                song.audio_url = result["url"]
            if not song.lyrics and isinstance(result.get("lrc"), str):
                song.lyrics = result["lrc"]
            cover_url = song.cover_url or result.get("pic")
        else:
            logger.warning(f"Unexpected NetEase song response: {result}")
            cover_url = song.cover_url
        if isinstance(cover_url, str) and cover_url:
            song.cover_url = await self._resolve_cover_url(cover_url)
        return song

    async def fetch_comments(self, song: Song) -> Song:
        """Fetch popular comments from NetEase's comment endpoint.

        Args:
            song: NetEase song whose comments should be fetched.

        Returns:
            The song with comments populated when available.
        """
        if song.comments:
            return song

        try:
            result = await self._request(
                url=f"https://music.163.com/weapi/v1/resource/hotcomments/R_SO_4_{song.id}?csrf_token=",
                method="POST",
                data={
                    "params": self.platform_cfg.enc_params if self.platform_cfg else "",
                    "encSecKey": self.platform_cfg.enc_sec_key
                    if self.platform_cfg
                    else "",
                },
            )
        except Exception as exc:
            logger.warning(f"{self.__class__.__name__} fetch_comments failed: {exc}")
            return song

        comments = result.get("hotComments") if isinstance(result, dict) else []
        if comments:
            song.comments = comments
        return song

    async def fetch_lyrics(self, song: Song) -> Song:
        """Fetch lyric text from the Meting lyric endpoint.

        Args:
            song: NetEase song whose lyrics should be fetched.

        Returns:
            The song with lyric text populated when available.
        """
        if song.lyrics:
            return song

        try:
            result = await self._request(
                self._build_api_url(server="netease", type="lrc", id=song.id)
            )
            if isinstance(result, str):
                song.lyrics = result
            elif isinstance(result, dict):
                lyric = result.get("lyric") or result.get("lrc")
                if isinstance(lyric, str):
                    song.lyrics = lyric
        except Exception as exc:
            logger.warning(f"{self.__class__.__name__} fetch_lyrics failed: {exc}")
        return song
