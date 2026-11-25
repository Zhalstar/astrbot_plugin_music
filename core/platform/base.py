import asyncio
import json
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
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

    _DEFAULT_AUDIO_SUFFIXES: ClassVar[frozenset[str]] = frozenset(
        {".mp3", ".flac", ".mflac", ".m4a", ".aac", ".ogg", ".opus", ".wav"}
    )
    _TEMP_SWEEP_INTERVAL_SECONDS: ClassVar[float] = 1800.0
    _TEMP_FILE_MAX_AGE_SECONDS: ClassVar[float] = 3600.0
    _last_temp_sweep_at: ClassVar[float] = 0.0

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.platform_cfg = getattr(config.players, self.platform.name)
        self.session: aiohttp.ClientSession | None = None
        self._owns_session = False
        self._session_factory: Callable[[], aiohttp.ClientSession] | None = None

    def set_session_factory(
        self, session_factory: Callable[[], aiohttp.ClientSession]
    ) -> None:
        """Attach a shared lazy HTTP session factory owned by MusicService."""
        self._session_factory = session_factory

    def __init_subclass__(cls, **kwargs):
        """自动注册子类到 _registry"""
        super().__init_subclass__(**kwargs)
        if ABC not in cls.__bases__ and cls not in BaseMusicPlayer._registry:  # 跳过抽象类
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

    async def download_cover(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> bytes | None:
        """Download a cover image using this platform's configured proxy.

        Args:
            url: Cover image URL.
            headers: Optional HTTP headers for platforms that require Referer/UA.

        Returns:
            Image bytes, or None if the download fails.
        """
        try:
            async with self._get_session().get(
                url,
                headers=headers,
                proxy=self.platform_cfg.proxy or None,
                timeout=aiohttp.ClientTimeout(total=20, connect=10),
            ) as response:
                if response.status != 200:
                    logger.warning(f"封面下载失败，HTTP 状态码：{response.status}")
                    return None
                return await response.read()
        except Exception as e:
            logger.warning(f"封面下载失败: {e}")
            return None

    @classmethod
    def cleanup_stale_temp_files(
        cls, temp_dir: Path, max_age_seconds: float = _TEMP_FILE_MAX_AGE_SECONDS
    ) -> int:
        """Remove expired files inside the plugin's dedicated temp directory."""
        cutoff = time.time() - max_age_seconds
        removed = 0
        try:
            if not temp_dir.is_dir():
                return 0
            entries = list(temp_dir.iterdir())
        except OSError as exc:
            logger.debug(f"Failed to scan temp directory {temp_dir}: {exc}")
            return 0
        for entry in entries:
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        return removed

    async def _download_to_temp(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        allowed_suffixes: frozenset[str] | None = None,
        timeout_seconds: float = 120,
    ) -> Path | None:
        """Stream an audio URL into the plugin temp directory."""
        suffixes = allowed_suffixes or self._DEFAULT_AUDIO_SUFFIXES
        self.cfg.temp_dir.mkdir(parents=True, exist_ok=True)
        now = time.monotonic()
        if now - BaseMusicPlayer._last_temp_sweep_at >= self._TEMP_SWEEP_INTERVAL_SECONDS:
            BaseMusicPlayer._last_temp_sweep_at = now
            await asyncio.to_thread(self.cleanup_stale_temp_files, self.cfg.temp_dir)

        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix not in suffixes:
            suffix = ".mp3"
        file_path = self.cfg.temp_dir / f"{uuid.uuid4().hex}{suffix}"
        try:
            async with self._get_session().get(
                url,
                headers=headers,
                proxy=self.platform_cfg.proxy or None,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds, connect=10),
            ) as response:
                if response.status != 200:
                    logger.warning(f"歌曲下载失败，HTTP 状态码：{response.status}")
                    return None
                async with aiofiles.open(file_path, "wb") as file:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        await file.write(chunk)
            logger.debug(f"歌曲下载完成，保存在：{file_path}")
            return file_path
        except asyncio.CancelledError:
            file_path.unlink(missing_ok=True)
            raise
        except Exception as e:
            file_path.unlink(missing_ok=True)
            logger.error(f"歌曲下载失败，错误信息：{e}")
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
        return await self._download_to_temp(song.audio_url)

    async def close(self):
        """Close the default HTTP session if it was created."""
        if self._owns_session and self.session and not self.session.closed:
            await self.session.close()

    def _get_session(self) -> aiohttp.ClientSession:
        """Return an injected/shared session or lazily create an isolated fallback.

        Returns:
            The adapter's configured session or a lazily created default session.
        """
        if self.session is not None and not self.session.closed:
            return self.session
        if self._session_factory is not None:
            return self._session_factory()
        self.session = aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=30, connect=10),
        )
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
