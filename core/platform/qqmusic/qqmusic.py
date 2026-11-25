import asyncio
import json
import re
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, ClassVar

import aiohttp

from astrbot.api import logger

from ...config import PluginConfig
from ...model import Platform, Playlist, Song
from ..base import BaseMusicPlayer


class QQMusic(BaseMusicPlayer):
    """QQ Music search and playback integration."""

    platform = Platform(
        name="qqmusic",
        display_name="QQ音乐",
    )

    API_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
    SONG_DETAIL_API_URL = "https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg"
    supports_playlist_import = True
    supported_audio_qualities: ClassVar[tuple[str, ...]] = (
        "standard",
        "exhigh",
        "lossless",
    )
    LYRIC_API_URL = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
    COMMENTS_API_URL = "https://c.y.qq.com/base/fcgi-bin/fcg_global_comment_h5.fcg"
    QUALITY_FILES: ClassVar[dict[str, tuple[tuple[str, str], ...]]] = {
        "standard": (("M500", "mp3"),),
        "exhigh": (("M800", "mp3"),),
        "lossless": (
            ("F000", "flac"),
            ("Q000", "flac"),
            ("Q001", "flac"),
            ("O801", "flac"),
        ),
    }
    URL_VKEY_FILES: ClassVar[dict[str, tuple[str, str]]] = {
        "standard": ("C400", "m4a"),
        "exhigh": ("M800", "mp3"),
        "lossless": ("F000", "flac"),
    }
    HEADERS: ClassVar[dict[str, str]] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0"
        ),
        "Referer": "https://y.qq.com/",
    }

    def __init__(self, config: PluginConfig):
        super().__init__(config)

    @staticmethod
    def hash33(value: str, seed: int = 5381) -> int:
        for char in value:
            seed += (seed << 5) + ord(char)
        return seed & 0x7FFFFFFF

    @staticmethod
    def _cookie_values(cookie: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for item in cookie.split(";"):
            key, separator, value = item.strip().partition("=")
            if separator:
                values[key] = value
        return values

    @classmethod
    def get_gtk(cls, cookie: str) -> int:
        values = cls._cookie_values(cookie)
        return cls.hash33(
            values.get("qqmusic_key")
            or values.get("qm_keyst")
            or values.get("p_skey")
            or ""
        )

    def _get_cookie(self) -> str:
        cookie = (self.platform_cfg.cookies or "").strip()
        values = self._cookie_values(cookie)
        if values.get("qqmusic_key") or values.get("qm_keyst"):
            return cookie
        return ""

    async def download_cover(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> bytes | None:
        """Download a QQ Music cover via the shared HTTP session.

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
        """Resolve and download the requested QQ Music audio quality.

        Args:
            song: Song whose audio URL should be downloaded.
            quality: Requested audio quality, falling back to standard.

        Returns:
            Path to the downloaded file, or None if the request fails.
        """
        requested_quality = quality or self.platform_cfg.quality or "standard"
        if requested_quality not in self.supported_audio_qualities:
            logger.warning(
                f"Unsupported QQ Music quality {requested_quality}; using standard"
            )
            requested_quality = "standard"
        if requested_quality != "standard" or not song.audio_url:
            await self._resolve_audio(song, requested_quality)
        if not song.audio_url:
            return None
        return await self._download_to_temp(
            song.audio_url,
            headers=self.HEADERS,
            allowed_suffixes=frozenset({".mp3", ".flac", ".m4a", ".aac"}),
        )

    @staticmethod
    def _get_response_song_list(data: dict[str, Any] | None) -> list[dict[str, Any]]:
        request = data.get("req") if isinstance(data, dict) else None
        response = request.get("data") if isinstance(request, dict) else None
        body = response.get("body") if isinstance(response, dict) else None
        if not isinstance(body, dict):
            return []
        song = body.get("item_song") or body.get("song")
        if not isinstance(song, dict):
            return []
        items = song.get("items") or song.get("list")
        return items if isinstance(items, list) else []

    def _song_from_item(self, item: dict[str, Any]) -> Song | None:
        """Normalize a QQ Music track returned by search, detail, or playlists.

        Args:
            item: QQ Music track payload.

        Returns:
            A normalized song, or None when the payload lacks a song MID.
        """
        song_mid = str(item.get("mid") or item.get("songmid") or "").strip()
        if not song_mid:
            return None
        singer_names = [
            str(singer["name"])
            for singer in item.get("singer", [])
            if isinstance(singer, dict) and singer.get("name")
        ]
        album = item.get("album") or {}
        if not isinstance(album, dict):
            album = {}
        album_mid = str(album.get("mid") or item.get("albummid") or "")
        interval = item.get("interval") or 0
        return Song(
            id=str(item.get("id") or item.get("songid") or song_mid),
            source=self.platform.name,
            card_type="qq",
            name=item.get("name") or item.get("songname"),
            artists="/".join(singer_names),
            duration=int(interval) * 1000 if interval else None,
            cover_url=(
                f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg"
                if album_mid
                else None
            ),
            page_url=f"https://y.qq.com/n/ryqq/songDetail/{song_mid}",
            audio_url=None,
            note=song_mid,
        )

    async def _get_json(
        self,
        url: str,
        params: dict[str, Any],
        *,
        cookie: str = "",
    ) -> dict[str, Any] | None:
        """Request a QQ Music JSON endpoint through the shared HTTP session.

        Args:
            url: Endpoint URL.
            params: Query parameters.
            cookie: Optional QQ Music Cookie header.

        Returns:
            The decoded JSON object, or None when the request fails.
        """
        headers = dict(self.HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        try:
            async with self._get_session().get(
                url,
                params=params,
                headers=headers,
                proxy=self.platform_cfg.proxy or None,
                timeout=aiohttp.ClientTimeout(total=15, connect=10),
            ) as response:
                if response.status != 200:
                    logger.warning(f"QQ Music API returned HTTP {response.status}")
                    return None
                raw = await response.read()
            result = json.loads(raw.decode("utf-8", "ignore"))
            return result if isinstance(result, dict) else None
        except Exception as exc:
            logger.warning(f"QQ Music API request failed: {exc}")
            return None

    async def _fetch_song_detail(self, song_mid: str) -> dict[str, Any] | None:
        """Fetch the current QQ Music track payload for a song MID.

        Args:
            song_mid: QQ Music string song MID.

        Returns:
            The first track payload, or None when unavailable.
        """
        result = await self._get_json(
            self.SONG_DETAIL_API_URL,
            {
                "songmid": song_mid,
                "tp": "yqq_song_detail",
                "format": "json",
            },
            cookie=self._get_cookie(),
        )
        items = result.get("data") if isinstance(result, dict) else None
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        return None

    async def _post_musicu(
        self, body: dict[str, Any], cookie: str
    ) -> dict[str, Any] | None:
        headers = dict(self.HEADERS)
        headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie

        try:
            async with self._get_session().post(
                self.API_URL,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                proxy=self.platform_cfg.proxy or None,
                timeout=aiohttp.ClientTimeout(total=15, connect=10),
            ) as response:
                if response.status != 200:
                    logger.warning(f"QQ Music API returned HTTP {response.status}")
                    return None
                raw = await response.read()
            result = json.loads(raw.decode("utf-8", "ignore"))
            return result if isinstance(result, dict) else None
        except Exception as exc:
            logger.warning(f"QQ Music API request failed: {exc}")
            return None

    async def fetch_songs(
        self, keyword: str, limit: int, extra: str | None = None
    ) -> list[Song]:
        """Search QQ Music with retries for transient empty responses.

        Args:
            keyword: Song or artist search text.
            limit: Maximum number of results to request, capped at 50 per page.
            extra: Unused platform-specific command context.

        Returns:
            Matching QQ Music songs.
        """
        del extra
        cookie = self._get_cookie()
        configured_uin = (self.platform_cfg.uin or "").strip() or "0"
        page_size = min(max(limit, 1), 50)
        requests = [
            {
                "comm": {"ct": "24", "cv": "0"},
                "req": {
                    "module": "music.adaptor.SearchAdaptor",
                    "method": "do_search_v2",
                    "param": {
                        "query": keyword,
                        "search_type": 0,
                        "page_num": 1,
                        "num_per_page": page_size,
                    },
                },
            },
            {
                "comm": {
                    "ct": "19",
                    "cv": "1859",
                    "uin": configured_uin,
                    "g_tk": self.get_gtk(cookie),
                },
                "req": {
                    "module": "music.search.SearchCgiService",
                    "method": "DoSearchForQQMusicDesktop",
                    "param": {
                        "num_per_page": page_size,
                        "page_num": 1,
                        "query": keyword,
                        "search_type": 0,
                    },
                },
            },
        ]
        for body in requests:
            for attempt in range(2):
                data = await self._post_musicu(body, cookie)
                items = self._get_response_song_list(data)
                if items:
                    songs = [self._song_from_item(item) for item in items]
                    return [song for song in songs if song is not None][:limit]
                if attempt == 0:
                    await asyncio.sleep(0.4)
        return []

    async def fetch_playlist(self, reference: str, limit: int = 1000) -> Playlist:
        """Fetch a QQ Music playlist through musicu.fcg.

        Args:
            reference: Numeric playlist ID or a y.qq.com playlist URL.
            limit: Maximum number of songs to import.

        Returns:
            Playlist metadata and normalized QQ Music songs.

        Raises:
            ValueError: If the reference or limit is invalid.
            RuntimeError: If QQ Music rejects or cannot find the playlist.
        """
        value = reference.strip()
        playlist_id = value if value.isdigit() else ""
        if not playlist_id:
            parsed = urllib.parse.urlparse(value)
            hostname = (parsed.hostname or "").lower()
            if parsed.scheme not in {"http", "https"} or not (
                hostname == "y.qq.com" or hostname.endswith(".y.qq.com")
            ):
                raise ValueError("invalid QQ Music playlist URL")
            query = urllib.parse.parse_qs(parsed.query)
            playlist_id = query.get("id", query.get("disstid", [""]))[0]
            if not playlist_id:
                match = re.search(r"/playlist/(\d+)", parsed.path)
                playlist_id = match.group(1) if match else ""
        if not playlist_id.isdigit():
            raise ValueError("QQ Music playlist ID must be numeric")
        if not 1 <= limit <= 1000:
            raise ValueError("playlist import limit must be between 1 and 1000")

        cookie = self._get_cookie()
        songs: list[Song] = []
        metadata: dict[str, Any] | None = None
        total = 0
        offset = 0
        seen_pages: set[tuple[str, ...]] = set()
        while len(songs) < limit:
            page_size = min(100, limit - len(songs))
            body = {
                "comm": {"ct": "24", "cv": "0"},
                "req": {
                    "module": "music.srfDissInfo.DissInfo",
                    "method": "CgiGetDiss",
                    "param": {
                        "disstid": int(playlist_id),
                        "dirid": 0,
                        "tag": False,
                        "song_begin": offset,
                        "song_num": page_size,
                        "userinfo": False,
                        "orderlist": False,
                        "onlysonglist": True,
                    },
                },
            }
            result = await self._post_musicu(body, cookie)
            response = result.get("req") if isinstance(result, dict) else None
            data = response.get("data") if isinstance(response, dict) else None
            if not isinstance(data, dict):
                raise RuntimeError("QQ Music could not load playlist")
            if metadata is None and isinstance(data.get("dirinfo"), dict):
                metadata = data["dirinfo"]
                total = int(metadata.get("songnum") or 0)  # type: ignore
            items = data.get("songlist")
            if not isinstance(items, list):
                raise RuntimeError("QQ Music returned an invalid playlist page")
            if not items:
                if total and offset < min(total, limit):
                    raise RuntimeError(
                        "QQ Music playlist ended before its declared total"
                    )
                break
            page_identity = tuple(
                str(item.get("mid") or item.get("songmid") or "")
                for item in items
                if isinstance(item, dict)
            )
            if page_identity in seen_pages:
                raise RuntimeError("QQ Music returned the same playlist page twice")
            seen_pages.add(page_identity)
            for item in items:
                if not isinstance(item, dict):
                    continue
                song = self._song_from_item(item)
                if song is not None:
                    songs.append(song)
                    if len(songs) >= limit:
                        break
            offset += len(items)
            if len(items) < page_size or total and offset >= total:
                break
        if not songs:
            raise RuntimeError("QQ Music playlist is empty or unavailable")
        metadata = metadata or {}
        creator = metadata.get("creator")
        return Playlist(
            id=playlist_id,
            source=self.platform.name,
            name=str(metadata.get("title") or f"QQ 音乐歌单 {playlist_id}"),
            songs=songs,
            cover_url=metadata.get("picurl"),
            creator=(
                creator.get("nick")
                if isinstance(creator, dict)
                else metadata.get("host_nick")
            ),
            description=metadata.get("desc"),
        )

    async def _fetch_preview_audio(self, song: Song, media_mid: str) -> Song:
        """Resolve an anonymous QQ Music preview URL.

        Args:
            song: Song whose preview URL should be resolved.
            media_mid: QQ Music media MID from the song detail response.

        Returns:
            The song with a preview URL when QQ Music provides one.
        """
        body = {
            "req_0": {
                "module": "music.vkey.GetVkey",
                "method": "UrlGetVkey",
                "param": {
                    "filename": [f"RS02{media_mid}.mp3"],
                    "guid": uuid.uuid4().hex,
                    "songmid": [song.note],
                    "uin": "0",
                    "loginflag": 0,
                    "platform": "20",
                },
            },
        }
        data = await self._post_musicu(body, "")
        request_data = data.get("req_0") if data else None
        response_data = (
            request_data.get("data") if isinstance(request_data, dict) else None
        )
        if not isinstance(response_data, dict):
            response_data = {}
        servers = response_data.get("sip") or []
        url_info = response_data.get("midurlinfo") or []
        if (
            servers
            and isinstance(url_info, list)
            and url_info
            and isinstance(url_info[0], dict)
            and url_info[0].get("purl")
        ):
            preview_url = self._media_url(str(servers[0]), str(url_info[0]["purl"]))
            if await self._probe_audio_url(preview_url):
                song.audio_url = preview_url
                song.quality = "preview"
                return song
        song.audio_url = None
        song.quality = None
        logger.warning(
            f"QQ Music did not return a playable preview URL for song {song.note}"
        )
        return song

    @staticmethod
    def _media_url(server: str, path: str) -> str:
        """Join a QQ Music media server and signed path using HTTPS.

        Args:
            server: Media server URL returned by QQ Music.
            path: Signed relative media path.

        Returns:
            A normalized absolute media URL.
        """
        url = f"{server.rstrip('/')}/{path.lstrip('/')}"
        return re.sub(r"^http:", "https:", url, flags=re.IGNORECASE)

    async def _probe_audio_url(self, url: str) -> bool:
        """Check that a signed QQ Music URL serves audio rather than an error body.

        Args:
            url: Signed media URL returned by a QQ Music vkey endpoint.

        Returns:
            True when a small range request returns plausible audio bytes.
        """
        headers = dict(self.HEADERS)
        headers["Range"] = "bytes=0-31"
        try:
            async with self._get_session().get(
                url,
                headers=headers,
                proxy=self.platform_cfg.proxy or None,
                timeout=aiohttp.ClientTimeout(total=8, connect=5),
            ) as response:
                if response.status not in {200, 206}:
                    return False
                content_type = str(response.headers.get("Content-Type") or "").lower()
                head = await response.content.read(32)
        except Exception as exc:
            logger.debug(f"QQ Music audio URL probe failed: {exc}")
            return False
        if not head or "json" in content_type or "text/" in content_type:
            return False
        is_audio_header = (
            head.startswith((b"ID3", b"fLaC", b"OggS", b"RIFF"))
            or len(head) >= 8
            and head[4:8] == b"ftyp"
            or len(head) >= 2
            and head[0] == 0xFF
            and head[1] & 0xE0 == 0xE0
        )
        return content_type.startswith("audio/") or is_audio_header

    def _apply_song_detail(self, song: Song, detail: dict[str, Any]) -> None:
        """Fill missing normalized fields from a QQ Music detail payload.

        Args:
            song: Song to update in place.
            detail: QQ Music track detail payload.
        """
        normalized = self._song_from_item(detail)
        if normalized is None:
            return
        song.id = normalized.id or song.id
        song.name = normalized.name or song.name
        song.artists = normalized.artists or song.artists
        song.duration = normalized.duration or song.duration
        song.cover_url = normalized.cover_url or song.cover_url
        song.page_url = normalized.page_url or song.page_url
        song.note = normalized.note or song.note

    async def _resolve_audio(self, song: Song, quality: str) -> Song:
        """Resolve an official QQ Music URL with quality and standard fallback.

        Args:
            song: Song carrying a QQ Music string MID in ``note``.
            quality: One of the adapter's supported audio quality identifiers.

        Returns:
            The song with an audio URL when an official endpoint provides one.
        """
        if not song.note:
            return song
        existing_audio_url = song.audio_url
        cookie = self._get_cookie()
        configured_uin = (self.platform_cfg.uin or "").strip() or "0"
        detail = await self._fetch_song_detail(song.note)
        if detail:
            self._apply_song_detail(song, detail)
        song_mid = str((detail or {}).get("mid") or song.note)
        file_info = (detail or {}).get("file") or {}
        media_mid = (
            str(file_info.get("media_mid") if isinstance(file_info, dict) else "")
            or song_mid
        )
        cookie_values = self._cookie_values(cookie)
        authst = cookie_values.get("qm_keyst") or cookie_values.get("qqmusic_key")
        qualities = [quality]
        if quality != "standard":
            qualities.append("standard")

        for candidate_quality in qualities:
            file_specs = self.QUALITY_FILES[candidate_quality]
            filenames = [
                f"{prefix}{media_mid}.{extension}" for prefix, extension in file_specs
            ]
            comm: dict[str, Any] = {
                "ct": 24,
                "cv": 4747474,
                "tmeAppID": "qqmusic",
                "format": "json",
                "uin": configured_uin,
            }
            params: dict[str, Any] = {
                "guid": uuid.uuid4().hex,
                "filename": filenames,
                "songmid": [song_mid],
                "songtype": [0],
                "uin": configured_uin,
                "loginflag": 1 if authst else 0,
                "platform": "20",
            }
            if authst:
                comm.update({"qq": configured_uin, "authst": authst})
            body = {
                "comm": comm,
                "req_0": {
                    "module": "vkey.GetVkeyServer",
                    "method": "CgiGetVkey",
                    "param": params,
                },
            }
            data = await self._post_musicu(body, cookie)
            request_data = data.get("req_0") if isinstance(data, dict) else None
            response_data = (
                request_data.get("data") if isinstance(request_data, dict) else None
            )
            if isinstance(response_data, dict):
                servers = response_data.get("sip") or []
                server = (
                    str(servers[0])
                    if isinstance(servers, list) and servers
                    else "https://wx.music.tc.qq.com/"
                )
                url_by_filename = {
                    str(item.get("filename")): str(item.get("purl"))
                    for item in response_data.get("midurlinfo") or []
                    if isinstance(item, dict)
                    and item.get("filename")
                    and item.get("purl")
                }
                for filename in filenames:
                    if purl := url_by_filename.get(filename):
                        media_url = self._media_url(server, purl)
                        if await self._probe_audio_url(media_url):
                            song.audio_url = media_url
                            song.quality = candidate_quality
                            return song

            prefix, extension = self.URL_VKEY_FILES[candidate_quality]
            filename = f"{prefix}{song_mid}.{extension}"
            body = {
                "req_0": {
                    "module": "music.vkey.GetVkey",
                    "method": "UrlGetVkey",
                    "param": {
                        "guid": uuid.uuid4().hex,
                        "songmid": [song_mid],
                        "filename": [filename],
                        "uin": configured_uin,
                        "loginflag": 1 if authst else 0,
                        "platform": "20",
                    },
                }
            }
            data = await self._post_musicu(body, cookie)
            request_data = data.get("req_0") if isinstance(data, dict) else None
            response_data = (
                request_data.get("data") if isinstance(request_data, dict) else None
            )
            if isinstance(response_data, dict):
                url_info = response_data.get("midurlinfo") or []
                first = url_info[0] if isinstance(url_info, list) and url_info else None
                if isinstance(first, dict) and first.get("purl"):
                    servers = response_data.get("sip") or []
                    server = (
                        str(servers[0])
                        if isinstance(servers, list) and servers
                        else "https://wx.music.tc.qq.com/"
                    )
                    media_url = self._media_url(server, str(first["purl"]))
                    if await self._probe_audio_url(media_url):
                        song.audio_url = media_url
                        song.quality = candidate_quality
                        return song

        logger.info(
            f"QQ Music did not return a {quality} audio URL for song {song_mid}; "
            "using the existing standard URL or preview"
        )
        if existing_audio_url:
            song.audio_url = existing_audio_url
            return song
        return await self._fetch_preview_audio(song, media_mid)

    async def fetch_extra(self, song: Song) -> Song:
        """Resolve a QQ Music full or preview URL using the official song MID.

        Args:
            song: Song returned by the QQ Music search API.

        Returns:
            The song with an audio URL when QQ Music provides one.
        """
        if song.audio_url or not song.note:
            return song
        return await self._resolve_audio(song, "standard")

    async def fetch_lyrics(self, song: Song) -> Song:
        """Fetch QQ Music lyrics by song ID, falling back to song MID.

        Args:
            song: Song returned by the QQ Music search API.

        Returns:
            The song with lyrics when QQ Music provides them.
        """
        if song.lyrics:
            return song

        identifiers = []
        if song.id:
            identifiers.append(("musicid", str(song.id)))
        if song.note and song.note != song.id:
            identifiers.append(("songmid", song.note))

        for identifier, value in identifiers:
            result = await self._get_json(
                self.LYRIC_API_URL,
                {
                    "g_tk": 5381,
                    "format": "json",
                    "inCharset": "utf-8",
                    "outCharset": "utf-8",
                    "notice": 0,
                    "platform": "h5",
                    "needNewCode": 1,
                    "ct": 121,
                    "cv": 0,
                    "nobase64": 1,
                    "songtype": 0,
                    identifier: value,
                },
                cookie=self._get_cookie(),
            )
            lyric = result.get("lyric") if isinstance(result, dict) else None
            if isinstance(lyric, str) and lyric.strip():
                song.lyrics = lyric
                return song

        return song

    async def fetch_comments(self, song: Song) -> Song:
        """Fetch popular comments for a QQ Music song.

        Args:
            song: Song returned by the QQ Music search API.

        Returns:
            The song with popular comments when QQ Music provides them.
        """
        if song.comments or not song.id:
            return song

        result = await self._get_json(
            self.COMMENTS_API_URL,
            {
                "g_tk": 0,
                "loginUin": 0,
                "hostUin": 0,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf-8",
                "notice": 0,
                "platform": "yqq.json",
                "needNewCode": 0,
                "cid": "205360772",
                "reqtype": 2,
                "biztype": 1,
                "topid": song.id,
                "cmd": 8,
                "needmusiccrit": 0,
                "pagenum": 0,
                "pagesize": 15,
            },
        )
        hot_comments = result.get("hot_comment") if isinstance(result, dict) else None
        comment_list = (
            hot_comments.get("commentlist") if isinstance(hot_comments, dict) else None
        )
        if isinstance(comment_list, list):
            song.comments = [
                {
                    "content": item.get("rootcommentcontent", ""),
                    "nickname": item.get("nick", ""),
                    "likedCount": item.get("praisenum", 0),
                }
                for item in comment_list
                if isinstance(item, dict) and item.get("rootcommentcontent")
            ]
        return song
