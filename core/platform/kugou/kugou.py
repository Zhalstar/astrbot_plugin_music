import base64
import binascii
import hashlib
import json
import time
import uuid
from typing import Any, ClassVar
from urllib.parse import quote

from astrbot.api import logger

from ...config import PluginConfig
from ...model import Platform, Song
from ..base import BaseMusicPlayer


class KuGouMusic(BaseMusicPlayer):
    """KuGou search and playback integration."""

    platform: ClassVar[Platform] = Platform(
        name="kugou",
        display_name="酷狗音乐",
    )

    APP_ID = "1005"
    CLIENT_VERSION = "20489"
    SIGN_SALT = "OIlwieks28dk2k092lksi2UIkp"
    SEARCH_URL = "https://complexsearch.kugou.com/v2/search/song"
    PLAY_URL = "https://gateway.kugou.com/v5/url"
    PRIV_URL = "https://gateway.kugou.com/v6/priv_url"
    HEADERS: ClassVar[dict[str, str]] = {
        "User-Agent": "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi",
        "Accept": "*/*",
    }

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        configured = self._cookie_values(self.platform_cfg.cookies or "")
        self._cookies = configured
        self._dfid = configured.get("dfid") or uuid.uuid4().hex.upper()[:24]
        configured_mid = configured.get("KUGOU_API_MID") or configured.get("mid")
        if configured_mid and configured_mid.isdigit():
            self._mid = configured_mid
        else:
            guid = str(uuid.uuid4())
            self._mid = str(int(hashlib.md5(guid.encode()).hexdigest(), 16))

    @staticmethod
    def _cookie_values(cookie: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for item in cookie.split(";"):
            key, separator, value = item.strip().partition("=")
            if separator and key:
                values[key] = value
        return values

    async def _request_kugou(
        self,
        url: str,
        params: dict[str, Any],
        router: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Send a signed request to a native KuGou endpoint.

        Args:
            url: KuGou endpoint URL.
            params: Query parameters to sign and send.
            router: Value for KuGou's x-router header.
            method: HTTP method used for the request.
            body: Optional JSON request body.

        Returns:
            Parsed JSON response, or None when the request fails.
        """
        try:
            headers = {
                **self.HEADERS,
                "x-router": router,
                "dfid": str(params.get("dfid", self._dfid)),
                "clienttime": str(params.get("clienttime", int(time.time()))),
                "mid": str(params.get("mid", self._mid)),
                "kg-rc": "1",
                "kg-thash": "5d816a0",
                "kg-rec": "1",
                "kg-rf": "B9EDA08A64250DEFFBCADDEE00F8F25F",
            }
            request_kwargs: dict[str, Any] = {
                "params": params,
                "headers": headers,
                "cookies": self._cookies,
                "proxy": self.platform_cfg.proxy or None,
                "timeout": 20,
            }
            if body is not None:
                request_kwargs["data"] = json.dumps(
                    body, ensure_ascii=False, separators=(",", ":")
                )
                headers["Content-Type"] = "application/json;charset=utf-8"
            async with self._get_session().request(
                method, url, **request_kwargs
            ) as response:
                if response.status != 200:
                    logger.warning(f"Native KuGou API returned HTTP {response.status}")
                    return None
                result = await response.json(content_type=None)
                return result if isinstance(result, dict) else None
        except Exception as exc:
            logger.warning(f"Native KuGou API request failed: {exc}")
            return None

    async def fetch_songs(
        self, keyword: str, limit: int, extra: str | None = None
    ) -> list[Song]:
        """Search songs through KuGou's signed Android endpoint.

        Args:
            keyword: Song or artist search text.
            limit: Maximum number of results to return.
            extra: Unused platform-specific command context.

        Returns:
            Matching KuGou songs.
        """
        del extra
        clienttime = str(int(time.time()))
        params: dict[str, Any] = {
            "dfid": self._dfid,
            "mid": self._mid,
            "uuid": "-",
            "appid": self.APP_ID,
            "clientver": self.CLIENT_VERSION,
            "clienttime": clienttime,
            "keyword": keyword,
            "page": 1,
            "pagesize": limit,
            "platform": "AndroidFilter",
            "iscorrection": 1,
            "privilegefilter": 0,
            "area_code": 1,
            "dopicfull": 1,
        }
        if self._cookies.get("token"):
            params["token"] = self._cookies["token"]
        if self._cookies.get("userid"):
            params["userid"] = self._cookies["userid"]
        params_string = "".join(f"{key}={params[key]}" for key in sorted(params))
        params["signature"] = hashlib.md5(
            f"{self.SIGN_SALT}{params_string}{self.SIGN_SALT}".encode()
        ).hexdigest()
        result = await self._request_kugou(
            self.SEARCH_URL, params, "complexsearch.kugou.com"
        )
        data = result.get("data") if isinstance(result, dict) else None
        items = data.get("lists", []) if isinstance(data, dict) else []
        songs: list[Song] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            song_hash = str(item.get("FileHash") or item.get("hash") or "")
            if not song_hash:
                continue
            cover = item.get("Image") or item.get("img") or ""
            cover = cover.replace("{size}", "400") if cover else ""
            if cover.startswith("//"):
                cover = f"https:{cover}"
            duration = item.get("Duration") or item.get("duration") or 0
            songs.append(
                Song(
                    id=song_hash,
                    source=self.platform.name,
                    card_type="kugou",
                    name=item.get("SongName") or item.get("songname") or "未知歌曲",
                    artists=item.get("SingerName") or item.get("singername") or "",
                    duration=int(duration) * 1000 if duration else None,
                    cover_url=cover or None,
                    page_url=f"https://www.kugou.com/song/#hash={quote(song_hash)}",
                    note=str(
                        item.get("AlbumID")
                        or item.get("album_id")
                        or item.get("AlbumAudioID")
                        or "0"
                    ),
                    album_id=int(item.get("AlbumID") or item.get("album_id") or 0)
                    or None,
                    album_audio_id=int(
                        item.get("AlbumAudioID")
                        or item.get("album_audio_id")
                        or item.get("MixSongID")
                        or item.get("mixsongid")
                        or 0
                    )
                    or None,
                )
            )
        return songs

    async def fetch_extra(self, song: Song) -> Song:
        """Resolve playback data and fill missing song metadata.

        Args:
            song: Song returned by the KuGou search endpoint.

        Returns:
            The song enriched with playback data when KuGou provides it.
        """
        configured = self._cookie_values(self.platform_cfg.cookies or "")
        if configured:
            self._cookies = configured
            self._dfid = configured.get("dfid") or self._dfid
            configured_mid = configured.get("KUGOU_API_MID") or configured.get("mid")
            if configured_mid and configured_mid.isdigit():
                self._mid = configured_mid
        if song.audio_url:
            return song

        if await self._try_fetch_vip_audio(song):
            return song

        data = await self._try_fetch_play_audio(song)
        if not isinstance(data, dict):
            return song

        trans_param = data.get("trans_param") or {}
        cover = data.get("img") or data.get("image") or trans_param.get("union_cover")
        if isinstance(cover, str) and cover:
            song.cover_url = song.cover_url or cover.replace("{size}", "400")
        song.name = song.name or data.get("fileName") or data.get("song_name")
        song.artists = song.artists or data.get("author_name")
        duration = data.get("timeLength")
        if duration and not song.duration:
            song.duration = int(duration) * 1000
        return song

    async def _try_fetch_vip_audio(self, song: Song) -> bool:
        """Try to resolve audio using KuGou's VIP endpoint.

        Args:
            song: Song whose playback URL should be resolved.

        Returns:
            Whether the VIP endpoint returned a playable URL.
        """
        clienttime = str(int(time.time()))
        userid = str(self._cookies.get("userid", "0"))
        priv_params: dict[str, Any] = {
            "dfid": self._dfid,
            "mid": self._mid,
            "uuid": "-",
            "appid": self.APP_ID,
            "clientver": self.CLIENT_VERSION,
            "clienttime": clienttime,
        }
        if self._cookies.get("token"):
            priv_params["token"] = self._cookies["token"]
        if self._cookies.get("userid"):
            priv_params["userid"] = userid
        resource: dict[str, Any] = {
            "collect_list_id": "3",
            "collect_time": int(time.time() * 1000),
            "hash": song.id.lower(),
            "id": 0,
            "page_id": 1,
            "type": "audio",
        }
        if song.album_audio_id:
            resource["album_audio_id"] = str(song.album_audio_id)
        vip_type_raw = self._cookies.get("vip_type", "0")
        vip_type_value = int(vip_type_raw) if vip_type_raw.isdigit() else 0
        priv_body: dict[str, Any] = {
            "area_code": "1",
            "behavior": "play",
            "qualities": [
                "128",
                "320",
                "flac",
                "high",
                "multitrack",
                "viper_atmos",
                "viper_tape",
                "viper_clear",
                "super",
            ],
            "resource": resource,
            "token": self._cookies.get("token", ""),
            "tracker_param": {
                "all_m": 1,
                "auth": "",
                "is_free_part": 0,
                "key": hashlib.md5(
                    f"{song.id.lower()}185672dd44712f60bb1736df5a377e82"
                    f"{self.APP_ID}{self._mid}{userid}".encode()
                ).hexdigest(),
                "module_id": 0,
                "need_climax": 1,
                "need_xcdn": 1,
                "open_time": "",
                "pid": "411",
                "pidversion": "3001",
                "priv_vip_type": "6",
                "viptoken": self._cookies.get("vip_token", ""),
            },
            "userid": userid,
            "vip": vip_type_value,
        }
        priv_params_string = "".join(
            f"{key}={priv_params[key]}" for key in sorted(priv_params)
        )
        body_string = json.dumps(priv_body, ensure_ascii=False, separators=(",", ":"))
        priv_params["signature"] = hashlib.md5(
            f"{self.SIGN_SALT}{priv_params_string}{body_string}{self.SIGN_SALT}".encode()
        ).hexdigest()
        if self._cookies.get("token") and self._cookies.get("userid"):
            result = await self._request_kugou(
                self.PRIV_URL,
                priv_params,
                "tracker.kugou.com",
                method="POST",
                body=priv_body,
            )
        else:
            logger.warning(
                "Skipping KuGou VIP URL request because native token/userid cookies are missing"
            )
            result = None
        data = result.get("data", result) if isinstance(result, dict) else None
        priv_items = data if isinstance(data, list) else [data]
        for item in priv_items:
            if not isinstance(item, dict):
                continue
            urls = item.get("url") or item.get("play_url") or item.get("playUrl")
            candidates = urls if isinstance(urls, list) else [urls]
            song.audio_url = next(
                (url for url in candidates if isinstance(url, str) and url.strip()),
                None,
            )
            if song.audio_url:
                return True

        return False

    async def _try_fetch_play_audio(self, song: Song) -> dict[str, Any] | None:
        """Fetch playback data from KuGou's standard URL endpoint.

        Args:
            song: Song whose playback URL and metadata should be resolved.

        Returns:
            The normalized endpoint data when available.
        """
        clienttime = str(int(time.time()))
        album_audio_id = song.album_audio_id or 0
        params: dict[str, Any] = {
            "dfid": self._dfid,
            "mid": self._mid,
            "uuid": "-",
            "appid": self.APP_ID,
            "clientver": "11430",
            "clienttime": clienttime,
            "album_id": song.album_id or 0,
            "area_code": 1,
            "hash": song.id.lower(),
            "ssa_flag": "is_fromtrack",
            "version": 11430,
            "page_id": 151369488,
            "quality": 128,
            "album_audio_id": album_audio_id,
            "behavior": "play",
            "pid": 2,
            "cmd": 26,
            "pidversion": 3001,
            "IsFreePart": 0,
            "ppage_id": "463467626,350369493,788954147",
            "cdnBackup": 1,
            "module": "",
        }
        userid = self._cookies.get("userid", "0")
        if self._cookies.get("token"):
            params["token"] = self._cookies["token"]
        if self._cookies.get("userid"):
            params["userid"] = userid
        params["key"] = hashlib.md5(
            f"{song.id.lower()}57ae12eb6890223e355ccfcb74edf70d{self.APP_ID}"
            f"{self._mid}{userid}".encode()
        ).hexdigest()
        params_string = "".join(f"{key}={params[key]}" for key in sorted(params))
        params["signature"] = hashlib.md5(
            f"{self.SIGN_SALT}{params_string}{self.SIGN_SALT}".encode()
        ).hexdigest()
        result = await self._request_kugou(
            self.PLAY_URL, params, "trackercdn.kugou.com"
        )
        data = result.get("data", result) if isinstance(result, dict) else None
        if isinstance(data, list):
            data = data[0] if data else None
        if isinstance(data, dict):
            urls = data.get("url") or data.get("play_url") or data.get("playUrl")
            backup_urls = data.get("backupUrl") or data.get("backup_url") or []
            candidates = urls if isinstance(urls, list) else [urls]
            if isinstance(backup_urls, list):
                candidates.extend(backup_urls)
            song.audio_url = next(
                (url for url in candidates if isinstance(url, str) and url.strip()),
                None,
            )
        else:
            data = None
        if not song.audio_url:
            status = result.get("status") if isinstance(result, dict) else None
            error_code = result.get("error_code") if isinstance(result, dict) else None
            error = result.get("error") if isinstance(result, dict) else None
            response_keys = sorted(result) if isinstance(result, dict) else []
            data_keys = sorted(data) if isinstance(data, dict) else []
            logger.warning(
                "Native KuGou API did not return audio for "
                f"{song.id} (status={status}, error_code={error_code}, "
                f"error={error}, response_keys={response_keys}, "
                f"data_type={type(data).__name__}, data_keys={data_keys})"
            )
        return data

    async def fetch_lyrics(self, song: Song) -> Song:
        """Fetch and decode KuGou lyrics for a song.

        Args:
            song: Song whose lyrics should be fetched.

        Returns:
            The song with decoded LRC lyrics when KuGou provides them.
        """
        if song.lyrics or not song.id:
            return song

        search_params: dict[str, Any] = {
            "album_audio_id": song.album_audio_id or 0,
            "appid": self.APP_ID,
            "clientver": self.CLIENT_VERSION,
            "duration": (song.duration or 0) // 1000,
            "hash": song.id,
            "keyword": f"{song.artists or ''} {song.name or ''}".strip(),
            "lrctxt": 1,
            "man": "no",
        }
        search_result = await self._request_kugou(
            "https://lyrics.kugou.com/v1/search",
            search_params,
            "lyrics.kugou.com",
        )
        search_body = (
            search_result.get("data", search_result)
            if isinstance(search_result, dict)
            else None
        )
        candidates = (
            search_body.get("info") or search_body.get("candidates") or []
            if isinstance(search_body, dict)
            else []
        )
        lyric_item = next(
            (
                item
                for item in candidates
                if isinstance(item, dict)
                and (item.get("id") or item.get("lyrics_id"))
                and item.get("accesskey")
            ),
            None,
        )
        if not lyric_item:
            return song

        clienttime = str(int(time.time()))
        download_params: dict[str, Any] = {
            "dfid": self._dfid,
            "mid": self._mid,
            "uuid": "-",
            "appid": self.APP_ID,
            "clientver": self.CLIENT_VERSION,
            "clienttime": clienttime,
            "ver": 1,
            "client": "android",
            "id": lyric_item.get("id") or lyric_item.get("lyrics_id"),
            "accesskey": lyric_item["accesskey"],
            "fmt": "lrc",
            "charset": "utf8",
        }
        if self._cookies.get("token"):
            download_params["token"] = self._cookies["token"]
        if self._cookies.get("userid"):
            download_params["userid"] = self._cookies["userid"]
        params_string = "".join(
            f"{key}={download_params[key]}" for key in sorted(download_params)
        )
        download_params["signature"] = hashlib.md5(
            f"{self.SIGN_SALT}{params_string}{self.SIGN_SALT}".encode()
        ).hexdigest()
        download_result = await self._request_kugou(
            "https://lyrics.kugou.com/download",
            download_params,
            "lyrics.kugou.com",
        )
        download_body = (
            download_result.get("data", download_result)
            if isinstance(download_result, dict)
            else None
        )
        content = (
            download_body.get("decodeContent") or download_body.get("content")
            if isinstance(download_body, dict)
            else None
        )
        if not isinstance(content, str) or not content.strip():
            return song
        if content.lstrip().startswith(("[", "{", "<?xml")):
            song.lyrics = content
            return song
        try:
            decoded = base64.b64decode(content).decode("utf-8", "ignore").strip()
        except (binascii.Error, ValueError):
            decoded = ""
        if decoded:
            song.lyrics = decoded
        return song

    async def fetch_comments(self, song: Song) -> Song:
        """Fetch popular comments from KuGou's song comment service.

        Args:
            song: Song whose comments should be fetched.

        Returns:
            The song with normalized comments when KuGou provides them.
        """
        if song.comments or not song.album_audio_id:
            return song

        clienttime = str(int(time.time()))
        params: dict[str, Any] = {
            "dfid": self._dfid,
            "mid": self._mid,
            "uuid": "-",
            "appid": self.APP_ID,
            "clientver": self.CLIENT_VERSION,
            "clienttime": clienttime,
            "kugouid": self._cookies.get("userid", "0"),
            "ver": 6,
            "clienttoken": self._cookies.get("token", ""),
            "mixsongid": str(song.album_audio_id),
            "need_show_image": 1,
            "p": 1,
            "pagesize": 30,
            "show_classify": 1,
            "show_hotword_list": 1,
            "extdata": "0",
            "code": "fc4be23b4e972707f36b8a828a93ba8a",
        }
        params["key"] = hashlib.md5(
            f"{self.APP_ID}{self.SIGN_SALT}{self.CLIENT_VERSION}{clienttime}".encode()
        ).hexdigest()
        if self._cookies.get("token"):
            params["token"] = self._cookies["token"]
        if self._cookies.get("userid"):
            params["userid"] = self._cookies["userid"]
        params_string = "".join(f"{key}={params[key]}" for key in sorted(params))
        params["signature"] = hashlib.md5(
            f"{self.SIGN_SALT}{params_string}{self.SIGN_SALT}".encode()
        ).hexdigest()
        result = await self._request_kugou(
            "https://gateway.kugou.com/mcomment/v1/cmtlist",
            params,
            "m.comment.service.kugou.com",
            method="POST",
        )
        body = result.get("data", result) if isinstance(result, dict) else None
        if not isinstance(body, dict):
            return song
        comment_list = (
            body.get("list")
            or body.get("comments")
            or body.get("commentlist")
            or body.get("hot_comments")
            or body.get("hotComments")
            or []
        )
        if not isinstance(comment_list, list):
            return song
        comments: list[dict[str, Any]] = []
        for item in comment_list:
            if not isinstance(item, dict):
                continue
            user = item.get("user") or item.get("userinfo") or {}
            content = (
                item.get("content")
                or item.get("comment_content")
                or item.get("rootcommentcontent")
            )
            if not isinstance(content, str) or not content.strip():
                continue
            comments.append(
                {
                    "content": content,
                    "nickname": item.get("nickname")
                    or item.get("nick")
                    or (user.get("nickname") if isinstance(user, dict) else "")
                    or "",
                    "likedCount": item.get("like_count")
                    or item.get("like_num")
                    or item.get("praisenum")
                    or 0,
                }
            )
        if comments:
            song.comments = comments
        return song
