import re
from typing import ClassVar
from urllib.parse import parse_qs, urlencode, urlparse

from astrbot.api import logger

from ..config import PluginConfig
from ..model import Platform, Playlist, Song
from .base import BaseMusicPlayer


class NetEaseMusicAPI(BaseMusicPlayer):
    """
    网易云音乐 NodeJS API 版
    """

    platform: ClassVar[Platform] = Platform(
        name="netease_api",
        display_name="网易云API版",
    )
    DEFAULT_BASE_URL = "https://zm.wwoyun.cn"
    base_url: str
    supports_playlist_import = True

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.base_url = (self.platform_cfg.base_url or self.DEFAULT_BASE_URL).rstrip(
            "/"
        )
        self.api_headers = dict(self.HEADERS)
        api_token = (self.platform_cfg.api_token or "").strip()
        if api_token:
            self.api_headers["Authorization"] = f"Bearer {api_token}"

    async def fetch_songs(self, keyword: str, limit: int = 5, extra=None) -> list[Song]:
        result = await self._request(
            url=f"{self.base_url}/search",
            method="POST",
            data={"keywords": keyword, "limit": limit, "type": 1, "offset": 0},
            headers=self.api_headers,
        )
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("result"), dict)
            or not isinstance(result["result"].get("songs"), list)
        ):
            logger.error(f"返回了意料之外数据：{result}")
            return []

        songs = result.get("result", {}).get("songs", [])[:limit]
        parsed_songs = []
        for item in songs:
            if not isinstance(item, dict):
                continue
            artists = item.get("artists") or item.get("ar") or []
            album = item.get("album") or item.get("al") or {}
            parsed_songs.append(
                Song(
                    id=str(item.get("id") or ""),
                    source=self.platform.name,
                    card_type="163",
                    name=item.get("name"),
                    artists="、".join(
                        artist["name"]
                        for artist in artists
                        if isinstance(artist, dict) and artist.get("name")
                    ),
                    duration=item.get("duration") or item.get("dt"),
                    cover_url=album.get("picUrl") if isinstance(album, dict) else None,
                    page_url=(
                        f"https://music.163.com/song?id={item['id']}"
                        if item.get("id")
                        else None
                    ),
                )
            )
        return parsed_songs

    async def fetch_playlist(self, reference: str, limit: int = 1000) -> Playlist:
        """Fetch a NetEase playlist through the configured NodeJS API.

        Args:
            reference: Numeric playlist ID or a music.163.com playlist URL.
            limit: Maximum number of songs to import.

        Returns:
            Playlist metadata and normalized NetEase songs.

        Raises:
            ValueError: If the reference or limit is invalid.
            RuntimeError: If playlist metadata or tracks cannot be loaded.
        """
        value = reference.strip()
        playlist_id = value if value.isdigit() else ""
        if not playlist_id:
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
                "music.163.com",
                "m.music.163.com",
            }:
                raise ValueError("invalid NetEase playlist URL")
            playlist_id = parse_qs(parsed.query).get("id", [""])[0]
            if not playlist_id:
                match = re.search(r"/playlist/(\d+)", parsed.path)
                playlist_id = match.group(1) if match else ""
        if not playlist_id.isdigit():
            raise ValueError("NetEase playlist ID must be numeric")
        if not 1 <= limit <= 1000:
            raise ValueError("playlist import limit must be between 1 and 1000")

        detail = await self._request(
            f"{self.base_url}/playlist/detail?{urlencode({'id': playlist_id})}",
            headers=self.api_headers,
        )
        metadata = detail.get("playlist") if isinstance(detail, dict) else None
        if not isinstance(metadata, dict) or not metadata.get("name"):
            raise RuntimeError("NetEase API could not load playlist details")
        result = await self._request(
            f"{self.base_url}/playlist/track/all?"
            f"{urlencode({'id': playlist_id, 'limit': limit, 'offset': 0})}",
            headers=self.api_headers,
        )
        items = result.get("songs") if isinstance(result, dict) else None
        if not isinstance(items, list):
            raise RuntimeError("NetEase API could not load playlist tracks")

        songs: list[Song] = []
        for item in items[:limit]:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            artists = item.get("artists") or item.get("ar") or []
            album = item.get("album") or item.get("al") or {}
            songs.append(
                Song(
                    id=str(item["id"]),
                    source=self.platform.name,
                    card_type="163",
                    name=item.get("name"),
                    artists="、".join(
                        artist["name"]
                        for artist in artists
                        if isinstance(artist, dict) and artist.get("name")
                    ),
                    duration=item.get("duration") or item.get("dt"),
                    cover_url=album.get("picUrl") if isinstance(album, dict) else None,
                    page_url=f"https://music.163.com/song?id={item['id']}",
                )
            )
        if not songs:
            raise RuntimeError("NetEase playlist is empty or unavailable")
        creator = metadata.get("creator")
        return Playlist(
            id=playlist_id,
            source=self.platform.name,
            name=str(metadata["name"]),
            songs=songs,
            cover_url=metadata.get("coverImgUrl"),
            creator=creator.get("nickname") if isinstance(creator, dict) else None,
            description=metadata.get("description"),
        )

    async def fetch_comments(self, song: Song) -> Song:
        if song.comments:
            return song
        result = await self._request(
            url=f"{self.base_url}/comment/hot",
            method="POST",
            data={"id": song.id, "type": 0},
            headers=self.api_headers,
        )
        if not isinstance(result, dict) or "hotComments" not in result:
            logger.error(f"返回了意料之外数据：{result}")
            return song
        if comments := result.get("hotComments"):
            song.comments = comments
        return song

    async def fetch_lyrics(self, song: Song) -> Song:
        if song.lyrics:
            return song
        result = await self._request(
            f"{self.base_url}/lyric?id={song.id}", headers=self.api_headers
        )
        if not isinstance(result, dict) or "lrc" not in result:
            logger.error(f"返回了意料之外数据：{result}")
            return song
        lyric = result["lrc"].get("lyric")
        if lyric:
            song.lyrics = lyric
        return song

    async def fetch_extra(self, song: Song) -> Song:
        try:
            result = await self._request(
                url=f"{self.base_url}/song/url?id={song.id}",
                method="GET",
                headers=self.api_headers,
            )
            if not isinstance(result, dict):
                logger.error(f"返回了意料之外数据：{result}")
                return song
        except Exception as e:
            logger.warning(f"{self.__class__.__name__} fetch_extra 失败: {e}")
            return song

        # NodeJS API 返回结构示例:
        # { "data": [ { "url": "...", ... } ] }
        data = result.get("data")
        if not isinstance(data, list) or not data:
            return song

        info = data[0]
        if not isinstance(info, dict):
            return song
        if info.get("id") is not None and str(info["id"]) != str(song.id):
            logger.warning("NodeJS API returned audio for an unexpected song")
            return song

        audio_url = info.get("url")
        trial = info.get("freeTrialInfo")
        short_audio = (
            isinstance(info.get("time"), (int, float))
            and song.duration is not None
            and song.duration > 60000
            and 0 < info["time"] < min(song.duration * 0.75, 65000)
        )
        if info.get("preview") or (trial and trial != "null") or short_audio:
            song.note = "当前音源仅提供试听片段，并非完整版。"
        elif info.get("unblocked"):
            source = str(info.get("source") or "未知")[:60]
            song.note = f"使用第三方替代音源（{source}），完整性及版本未核验。"
        if audio_url and song.audio_url is None:
            song.audio_url = audio_url

        return song
