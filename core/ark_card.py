import asyncio
import json
from collections.abc import Callable
from typing import Any

import aiohttp

from astrbot.api import logger

from .config import PluginConfig
from .model import Song
from .platform import BaseMusicPlayer


class ArkCard:
    """Fetch signed music cards from configurable Ark-compatible services."""

    API_URL = "https://apii.xianyuw.cn/api/v1/qq-musicArk"
    FORMAT_MAP = {"163": "netease"}
    SUPPORTED_CARD_TYPES = {"qq", "163", "kugou", "bilibili"}
    REQUIRED_FIELDS = {"app", "meta", "prompt", "view"}

    def __init__(
        self,
        config: PluginConfig,
        session_factory: Callable[[], aiohttp.ClientSession] | None = None,
    ):
        self.key = config.send_config.ark_key
        self._timeout = config.search_config.timeout
        self._session_factory = session_factory
        self.session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        """Return the shared HTTP session or lazily create a local fallback."""
        if self._session_factory is not None:
            return self._session_factory()
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            )
        return self.session

    async def close(self) -> None:
        if self.session is not None and not self.session.closed:
            await self.session.close()
        self.session = None

    async def _resolve_cover_url(self, cover_url: str, proxy: str | None) -> str:
        """Resolve a cover URL to its final HTTP redirect target.

        Args:
            cover_url: Original cover image URL.
            proxy: Proxy URL for the current music source, if configured.

        Returns:
            The final redirected URL, or the original URL when resolution fails.
        """
        try:
            async with self._get_session().get(
                cover_url,
                allow_redirects=True,
                proxy=proxy or None,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as cover_response:
                cover_response.raise_for_status()
                return str(cover_response.url)
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
            logger.warning(
                f"Ark card cover redirect failed ({type(exc).__name__}); "
                "using the original URL"
            )
            return cover_url

    async def fetch(self, player: BaseMusicPlayer, song: Song) -> dict[str, Any] | None:
        """Fetch a signed Ark music card.

        Args:
            player: Music player that produced the song.
            song: Song data used to build the card.

        Returns:
            The signed card payload, or None when the request fails.
        """
        if not song.audio_url:
            logger.warning(f"Ark card is missing required song data: {song.name}")
            return None
        singer = song.artists or "Unknown artist"

        proxy = player.platform_cfg.proxy if player.platform_cfg else None
        if song.card_type not in self.SUPPORTED_CARD_TYPES:
            logger.warning(f"Ark card does not support card type: {song.card_type}")
            return None
        card_format = self.FORMAT_MAP.get(song.card_type, song.card_type)

        if not song.cover_url:
            song = await player.fetch_extra(song)
        cover_url = (
            await self._resolve_cover_url(song.cover_url, proxy)
            if song.cover_url
            else ""
        )

        params = {
            "key": self.key,
            "url": song.audio_url,
            "song": song.name,
            "singer": singer,
            "cover": cover_url,
            "jump": song.page_url or song.audio_url,
            "format": card_format,
        }
        try:
            async with self._get_session().get(
                self.API_URL,
                params=params,
                proxy=proxy or None,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as response:
                response.raise_for_status()
                result = await response.json(content_type=None)
        except (
            aiohttp.ClientError,
            TimeoutError,
            asyncio.TimeoutError,
            json.JSONDecodeError,
        ) as exc:
            logger.warning(f"Ark card request failed: {type(exc).__name__}")
            return None

        data = result.get("data") if isinstance(result, dict) else None
        if (
            not isinstance(result, dict)
            or result.get("code") != 200
            or not isinstance(data, dict)
            or not self.REQUIRED_FIELDS.issubset(data)
        ):
            logger.warning("Ark card returned an invalid payload")
            return None
        return data
