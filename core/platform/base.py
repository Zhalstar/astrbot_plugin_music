import json
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

import aiofiles
import aiohttp

from astrbot.api import logger

from ..config import MusicPlayerItem, PluginConfig
from ..model import Platform, Playlist, Song


class BaseMusicPlayer(ABC):
    """
    全功能音乐平台基类 + HTTP 支持
    子类必须实现：
    - platform: 平台信息（包含名称和显示名称)
    - fetch_songs: 获取歌曲列表
    """

    _registry: ClassVar[list[type["BaseMusicPlayer"]]] = []
    """ 存储所有已注册的 MusicPlatform 类 """

    platform: ClassVar[Platform]
    """ 平台信息（包含名称和显示名称） """
    supported_audio_qualities: ClassVar[tuple[str, ...]] = ("standard",)
    """Audio quality identifiers supported by this platform."""
    supports_playlist_import: ClassVar[bool] = False
    """Whether this adapter can import a remote playlist."""
    platform_cfg: MusicPlayerItem

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; WOW64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/55.0.2883.87 Safari/537.36"
        )
    }

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.platform_cfg = getattr(config.players, self.platform.name)
        self.session: aiohttp.ClientSession | None = None
        self._owns_session = False

    def __init_subclass__(cls, **kwargs):
        """自动注册子类到 _registry"""
        super().__init_subclass__(**kwargs)
        if ABC not in cls.__bases__:  # 跳过抽象类
            BaseMusicPlayer._registry.append(cls)

    @classmethod
    def get_all_subclass(cls) -> list[type["BaseMusicPlayer"]]:
        """获取所有已注册的 Parser 类"""
        return cls._registry

    # ---------- 子类必须实现 ----------

    @abstractmethod
    async def fetch_songs(
        self, keyword: str, limit: int, extra: str | None = None
    ) -> list[Song]:
        """
        搜索歌曲
        :param keyword: 搜索关键字
        :param limit: 搜索数量
        :param extra: 额外参数
        """
        raise NotImplementedError

    # ---------- 可复用方法 ----------
    async def fetch_playlist(self, reference: str, limit: int = 1000) -> Playlist:
        """Fetch a remote playlist when the adapter supports it.

        Args:
            reference: Platform playlist ID or a recognized share URL.
            limit: Maximum number of songs to return.

        Returns:
            Normalized playlist metadata and songs.

        Raises:
            NotImplementedError: If this adapter has no playlist integration.
        """
        del reference, limit
        raise NotImplementedError(
            f"{self.platform.display_name} does not support playlist import"
        )

    @abstractmethod
    async def fetch_extra(self, song: Song) -> Song:
        """Fetch platform-specific metadata required for sending a song.

        Args:
            song: Song whose metadata should be completed.

        Returns:
            The song with platform-specific metadata populated.

        Raises:
            NotImplementedError: If a platform does not implement metadata fetching.
        """
        raise NotImplementedError

    @abstractmethod
    async def fetch_comments(self, song: Song) -> Song:
        """Fetch platform-specific popular comments for a song.

        Args:
            song: Song whose comments should be fetched.

        Returns:
            The song with comments populated when available.

        Raises:
            NotImplementedError: If a platform does not implement comments.
        """
        raise NotImplementedError

    @abstractmethod
    async def fetch_lyrics(self, song: Song) -> Song:
        """Fetch platform-specific lyrics for a song.

        Args:
            song: Song whose lyrics should be fetched.

        Returns:
            The song with lyrics populated when available.

        Raises:
            NotImplementedError: If a platform does not implement lyrics.
        """
        raise NotImplementedError

    async def resolve_lyrics(self, song: Song) -> Song:
        """将歌词 URL 解析为歌词正文。"""
        lyrics = song.lyrics.strip() if isinstance(song.lyrics, str) else ""
        if not lyrics.startswith(("http://", "https://")):
            return song

        try:
            async with self._get_session().get(
                lyrics,
                headers=self.HEADERS,
                proxy=self.platform_cfg.proxy or None,
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"歌词 URL 请求返回 {resp.status}: {lyrics}")
                    return song

                content = (await resp.text()).strip("\ufeff").strip()
                logger.debug(f"已成功解析歌词URL: {lyrics}")
                if content:
                    song.lyrics = content
        except Exception as e:
            logger.warning(f"{self.__class__.__name__} resolve_lyrics 失败: {e}")

        return song

    async def download_cover(self, url: str) -> bytes | None:
        """Download a cover image using this platform's configured proxy.

        Args:
            url: Cover image URL.

        Returns:
            Image bytes, or None if the download fails.
        """
        try:
            async with self._get_session().get(
                url, proxy=self.platform_cfg.proxy or None
            ) as response:
                if response.status != 200:
                    logger.warning(f"封面下载失败，HTTP 状态码：{response.status}")
                    return None
                return await response.read()
        except Exception as e:
            logger.warning(f"封面下载失败: {e}")
            return None

    async def download_audio(
        self, song: Song, quality: str | None = None
    ) -> Path | None:
        """Download a song's audio to the shared temp directory.

        Args:
            song: Song whose resolved audio URL should be downloaded.
            quality: Requested audio quality, when supported by the platform.

        Returns:
            Path to the downloaded file, or None if the download fails.
        """
        del quality
        if not song.audio_url:
            return None

        url = song.audio_url
        self.cfg.temp_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix not in {
            ".mp3",
            ".flac",
            ".mflac",
            ".m4a",
            ".aac",
            ".ogg",
            ".opus",
            ".wav",
        }:
            suffix = ".mp3"
        file_path = self.cfg.temp_dir / f"{uuid.uuid4().hex}{suffix}"
        try:
            async with self._get_session().get(
                url, proxy=self.platform_cfg.proxy or None
            ) as response:
                if response.status != 200:
                    logger.warning(f"歌曲下载失败，HTTP 状态码：{response.status}")
                    return None
                async with aiofiles.open(file_path, "wb") as file:
                    async for chunk in response.content.iter_chunked(1024):
                        await file.write(chunk)
            logger.debug(f"歌曲下载完成，保存在：{file_path}")
            return file_path
        except Exception as e:
            file_path.unlink(missing_ok=True)
            logger.error(f"歌曲下载失败，错误信息：{e}")
            return None

    async def close(self):
        """Close the default HTTP session if it was created."""
        if self._owns_session and self.session and not self.session.closed:
            await self.session.close()

    def _get_session(self) -> aiohttp.ClientSession:
        """Create the default session on demand.

        Returns:
            The adapter's configured session or a lazily created default session.
        """
        if self.session is None:
            self.session = aiohttp.ClientSession()
            self._owns_session = True
        return self.session

    # ---------- 内部 HTTP 方法 ----------

    async def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: dict | None = None,
        headers: dict | None = None,
        cookies: dict | None = None,
        ssl: bool = True,
    ):
        headers = headers or self.HEADERS
        proxy = self.platform_cfg.proxy or None
        session = self._get_session()

        if method.upper() == "POST":
            async with session.post(
                url,
                data=data,
                headers=headers,
                cookies=cookies,
                ssl=ssl,
                proxy=proxy or None,
            ) as resp:
                return await self._parse_response(resp)

        async with session.get(
            url, headers=headers, cookies=cookies, ssl=ssl, proxy=proxy or None
        ) as resp:
            return await self._parse_response(resp)

    async def _parse_response(self, resp: aiohttp.ClientResponse):
        try:
            resp_text = await resp.text()

            if resp.status != 200:
                logger.warning(f"HTTP 请求返回 {resp.status}: {resp_text[:200]}")
                return None

            if not resp_text.strip():
                logger.warning("HTTP 响应为空")
                return None

            try:
                return json.loads(resp_text)
            except json.JSONDecodeError:
                return resp_text

        except Exception as e:
            logger.warning(f"解析响应失败: {e}")
            return None
