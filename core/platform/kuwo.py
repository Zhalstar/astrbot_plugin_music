import asyncio
import re
import secrets
import string
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlencode, urlparse

from astrbot.api import logger

from ..model import Platform, Playlist, Song
from .base import BaseMusicPlayer


class KuwoMusic(BaseMusicPlayer):
    """Search Kuwo and resolve playable track URLs."""

    platform: ClassVar[Platform] = Platform(
        name="kuwo",
        display_name="酷我音乐",
    )

    SEARCH_URL = "http://search.kuwo.cn/r.s"
    PLAYLIST_URL = "https://nplserver.kuwo.cn/pl.svc"
    TRACK_URL = "https://changenotice.kuwo.cn/mobi.s"
    DETAIL_URL = "http://musicpay.kuwo.cn/music.pay"
    SEARCH_API_URL = SEARCH_URL
    TRACK_API_URL = TRACK_URL
    DETAIL_API_URL = DETAIL_URL
    TRACK_SOURCE = "kwplayer_ar_8.5.5.0_keluze.apk"
    TRACK_BITRATE = "128kmp3"
    QUALITY_TO_BITRATE: ClassVar[dict[str, str]] = {
        "standard": "128kmp3",
        "exhigh": "320kmp3",
        "lossless": "2000kflac",
        "hires": "4000kflac",
        "hifi": "20201kmflac",
        "sur": "20501kmflac",
        "jymaster": "20900kmflac",
    }
    supported_audio_qualities: ClassVar[tuple[str, ...]] = tuple(QUALITY_TO_BITRATE)
    supports_playlist_import = True
    TRACK_USER_ALPHABET = string.ascii_lowercase + string.digits
    TRACK_USER_LENGTH = 16
    COVER_BASE_URL = "http://img1.kwcdn.kuwo.cn/star/albumcover/"
    MUSICRID_PATTERN = re.compile(r"(?:MUSIC_)?(?P<id>\d+)$", re.IGNORECASE)

    async def fetch_songs(
        self,
        keyword: str,
        limit: int,
        extra: str | None = None,
    ) -> list[Song]:
        """Search Kuwo's public search endpoint.

        Args:
            keyword: Song or artist search text.
            limit: Maximum number of songs to return.
            extra: Unused platform-specific command context.

        Returns:
            Matching Kuwo songs.
        """
        del extra
        limit = max(1, limit)
        params = {
            "allpay": 1,
            "all": keyword,
            "pn": 0,
            "rn": limit,
            "vipver": 1,
            "show_copyright_off": 1,
            "correct": 1,
            "ft": "music",
            "encoding": "utf8",
            "rformat": "json",
            "vermerge": 1,
            "mobi": 1,
            "issubtitle": 1,
        }
        try:
            result = await self._request(
                f"{self.SEARCH_URL}?{urlencode(params)}",
                headers={
                    **self.HEADERS,
                    "Accept": "application/json, text/plain, */*",
                },
            )
        except Exception as exc:
            logger.warning(f"Kuwo search request failed: {exc}")
            return []

        items = result.get("abslist") if isinstance(result, dict) else None
        if not isinstance(items, list):
            logger.warning("Kuwo search returned an invalid result list")
            return []

        songs: list[Song] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_id = str(item.get("MUSICRID") or item.get("musicrid") or "").strip()
            match = self.MUSICRID_PATTERN.fullmatch(raw_id)
            if not match:
                continue

            duration = item.get("DURATION") or item.get("duration")
            duration_ms: int | None = None
            try:
                if isinstance(duration, str) and ":" in duration:
                    minutes, seconds = duration.split(":", 1)
                    duration_ms = (int(minutes) * 60 + int(seconds)) * 1000
                elif duration is not None:
                    duration_ms = int(float(duration) * 1000)
            except (TypeError, ValueError):
                logger.debug(f"Kuwo returned an invalid duration: {duration!r}")

            cover = item.get("web_albumpic_short") or item.get("web_album_cover_short")
            if isinstance(cover, str):
                cover = cover.strip()
                if cover.startswith("//"):
                    cover = f"https:{cover}"
                elif cover and not cover.startswith(("http://", "https://")):
                    cover = f"{self.COVER_BASE_URL}{cover.lstrip('/')}"
            else:
                cover = None

            song_id = match.group("id")
            songs.append(
                Song(
                    id=song_id,
                    source=self.platform.name,
                    card_type="kuwo",
                    name=item.get("NAME") or item.get("name") or "Unknown song",
                    artists=item.get("ARTIST") or item.get("artist") or "",
                    duration=duration_ms,
                    cover_url=cover or None,
                    page_url=f"https://www.kuwo.cn/play_detail/{song_id}",
                    note=item.get("ALBUM") or item.get("album") or None,
                )
            )
            if len(songs) >= limit:
                break
        return songs

    async def fetch_playlist(self, reference: str, limit: int = 1000) -> Playlist:
        """Fetch a Kuwo playlist from its public playlist service.

        Args:
            reference: Numeric playlist ID or a kuwo.cn playlist URL.
            limit: Maximum number of songs to import.

        Returns:
            Playlist metadata and normalized Kuwo songs.

        Raises:
            ValueError: If the reference or limit is invalid.
            RuntimeError: If Kuwo does not return valid playlist data.
        """
        value = reference.strip()
        playlist_id = value if value.isdigit() else ""
        if not playlist_id:
            parsed = urlparse(value)
            hostname = (parsed.hostname or "").lower()
            if parsed.scheme not in {"http", "https"} or not (
                hostname == "kuwo.cn" or hostname.endswith(".kuwo.cn")
            ):
                raise ValueError("invalid Kuwo playlist URL")
            query = parse_qs(parsed.query)
            playlist_id = query.get("pid", query.get("id", [""]))[0]
            if not playlist_id:
                match = re.search(r"/(?:playlist_detail|playlist)/(\d+)", parsed.path)
                playlist_id = match.group(1) if match else ""
        if not playlist_id.isdigit():
            raise ValueError("Kuwo playlist ID must be numeric")
        if not 1 <= limit <= 1000:
            raise ValueError("playlist import limit must be between 1 and 1000")

        result = await self._request(
            f"{self.PLAYLIST_URL}?"
            f"{urlencode({'op': 'getlistinfo', 'pid': playlist_id, 'pn': 0, 'rn': limit, 'encode': 'utf-8', 'keyset': 'pl2012', 'identity': 'kuwo', 'pcmp4': 1, 'vipver': 1})}"
        )
        items = result.get("musiclist") if isinstance(result, dict) else None
        if (
            not isinstance(result, dict)
            or result.get("result") != "ok"
            or not isinstance(items, list)
        ):
            raise RuntimeError("Kuwo playlist service returned invalid data")

        songs: list[Song] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            raw_id = str(
                item.get("id") or item.get("musicrid") or item.get("MUSICRID") or ""
            ).strip()
            match = self.MUSICRID_PATTERN.fullmatch(raw_id)
            if not match:
                continue
            duration = item.get("duration")
            try:
                duration_ms = int(float(duration)) * 1000 if duration else None
            except (TypeError, ValueError):
                duration_ms = None
            cover = item.get("albumpic") or item.get("musicPic")
            if isinstance(cover, str):
                cover = cover.strip()
                if cover.startswith("http://"):
                    cover = f"https://{cover[7:]}"
            else:
                cover = None
            song_id = match.group("id")
            songs.append(
                Song(
                    id=song_id,
                    source=self.platform.name,
                    card_type="kuwo",
                    name=item.get("name") or item.get("FSONGNAME") or "Unknown song",
                    artists=item.get("artist") or item.get("FARTIST") or "",
                    duration=duration_ms,
                    cover_url=cover or None,
                    page_url=f"https://www.kuwo.cn/play_detail/{song_id}",
                    note=item.get("album") or item.get("FALBUM") or None,
                )
            )
        if not songs:
            raise RuntimeError("Kuwo playlist is empty or unavailable")

        cover_url = result.get("pic")
        if isinstance(cover_url, str) and cover_url.startswith("http://"):
            cover_url = f"https://{cover_url[7:]}"
        return Playlist(
            id=playlist_id,
            source=self.platform.name,
            name=str(result.get("title") or f"酷我歌单 {playlist_id}"),
            songs=songs,
            cover_url=cover_url if isinstance(cover_url, str) else None,
            creator=str(result.get("uname")) if result.get("uname") else None,
            description=str(result.get("info")) if result.get("info") else None,
        )

    async def fetch_extra(self, song: Song) -> Song:
        """Resolve a Kuwo direct audio URL and enrich track metadata.

        Args:
            song: Song returned by :meth:`fetch_songs`.

        Returns:
            The song with any available playback and detail metadata.
        """
        if song.audio_url:
            return song

        track_params = {
            "f": "web",
            "source": self.TRACK_SOURCE,
            "type": "convert_url_with_sign",
            "rid": song.id,
            "br": self.TRACK_BITRATE,
            "user": "".join(
                secrets.choice(self.TRACK_USER_ALPHABET)
                for _ in range(self.TRACK_USER_LENGTH)
            ),
        }
        detail_params = {
            "src": "kwplayer_ar_11.3.1.1_40.apk",
            "op": "query",
            "action": "play",
            "ids": song.id,
        }

        try:
            track_result, detail_result = await asyncio.gather(
                self._request(f"{self.TRACK_URL}?{urlencode(track_params)}"),
                self._request(f"{self.DETAIL_URL}?{urlencode(detail_params)}"),
                return_exceptions=True,
            )
        except Exception as exc:
            logger.warning(f"Kuwo media request failed: {exc}")
            return song

        if isinstance(track_result, Exception):
            logger.warning(f"Kuwo track request failed: {track_result}")
            track_result = None
        if isinstance(detail_result, Exception):
            logger.debug(f"Kuwo detail request failed: {detail_result}")
            detail_result = None

        track_data = (
            track_result.get("data")
            if isinstance(track_result, dict) and str(track_result.get("code")) == "200"
            else None
        )
        if isinstance(track_data, dict):
            audio_url = track_data.get("url")
            if isinstance(audio_url, str) and audio_url:
                song.audio_url = audio_url.split("?", 1)[0]
            if not song.duration:
                duration_value = track_data.get("duration")
                if isinstance(duration_value, (int, float, str)) and not isinstance(
                    duration_value, bool
                ):
                    try:
                        song.duration = int(float(duration_value)) * 1000
                    except (TypeError, ValueError):
                        pass
        else:
            logger.warning(f"Kuwo did not return an audio URL for song {song.id}")

        detail_songs = (
            detail_result.get("songs")
            if isinstance(detail_result, dict)
            and str(detail_result.get("errorcode")) == "0"
            and str(detail_result.get("result", "")).lower() == "ok"
            else None
        )
        detail = (
            detail_songs[0] if isinstance(detail_songs, list) and detail_songs else None
        )
        if isinstance(detail, dict):
            song.name = detail.get("name") or song.name
            song.artists = detail.get("artist") or song.artists
            song.note = detail.get("album") or song.note
            song.cover_url = detail.get("albumPic") or song.cover_url
            if not song.duration:
                duration_value = detail.get("duration")
                if isinstance(duration_value, (int, float, str)) and not isinstance(
                    duration_value, bool
                ):
                    try:
                        song.duration = int(float(duration_value)) * 1000
                    except (TypeError, ValueError):
                        pass
        return song

    async def download_audio(
        self, song: Song, quality: str | None = None
    ) -> Path | None:
        """Download configured Kuwo quality, falling back to standard audio.

        Args:
            song: Song with a standard-quality fallback URL.

        Returns:
            Path to the requested or fallback audio file, or None if both
            downloads fail.
        """
        quality = quality or self.platform_cfg.quality or "lossless"
        bitrate = self.QUALITY_TO_BITRATE.get(quality, self.TRACK_BITRATE)
        if bitrate != self.TRACK_BITRATE and song.id:
            track_params = {
                "f": "web",
                "source": self.TRACK_SOURCE,
                "type": "convert_url_with_sign",
                "rid": song.id,
                "br": bitrate,
                "user": "".join(
                    secrets.choice(self.TRACK_USER_ALPHABET)
                    for _ in range(self.TRACK_USER_LENGTH)
                ),
            }
            try:
                track_result = await self._request(
                    f"{self.TRACK_URL}?{urlencode(track_params)}"
                )
            except Exception as exc:
                logger.warning(
                    f"Kuwo {quality} quality request failed, falling back: {exc}"
                )
            else:
                track_data = (
                    track_result.get("data")
                    if isinstance(track_result, dict)
                    and str(track_result.get("code")) == "200"
                    else None
                )
                high_url = (
                    track_data.get("url") if isinstance(track_data, dict) else None
                )
                audio_format = (
                    str(track_data.get("format", "")).strip().lower()
                    if isinstance(track_data, dict)
                    else ""
                )
                ekey = track_data.get("ekey") if isinstance(track_data, dict) else None
                clean_high_url = (
                    high_url.split("?", 1)[0]
                    if isinstance(high_url, str) and high_url
                    else ""
                )
                is_mflac = (
                    audio_format == "mflac"
                    or Path(clean_high_url).suffix.lower() == ".mflac"
                )
                if clean_high_url and (
                    not is_mflac or (isinstance(ekey, str) and ekey)
                ):
                    try:
                        file_path = await super().download_audio(
                            Song(id=song.id, audio_url=clean_high_url)
                        )
                    except Exception as exc:
                        logger.warning(
                            f"Kuwo {quality} quality download failed, falling back: {exc}"
                        )
                    else:
                        if file_path:
                            if is_mflac:
                                decrypted_path = file_path.with_suffix(".flac")
                                try:
                                    from nonebot_plugin_kuwo.qmc import (  # type: ignore
                                        decrypt_mflac_file,
                                    )

                                    await asyncio.to_thread(
                                        decrypt_mflac_file,
                                        file_path,
                                        decrypted_path,
                                        ekey,
                                    )
                                    if (
                                        decrypted_path.is_file()
                                        and decrypted_path.stat().st_size > 0
                                    ):
                                        file_path.unlink(missing_ok=True)
                                        return decrypted_path
                                except Exception as exc:
                                    logger.warning(
                                        "Kuwo MFLAC decrypt failed, falling back: "
                                        f"{exc}"
                                    )
                                    decrypted_path.unlink(missing_ok=True)
                                file_path.unlink(missing_ok=True)
                            else:
                                return file_path
                logger.warning(
                    f"Kuwo {quality} quality unavailable for {song.id}, "
                    "falling back to standard quality"
                )

        if song.audio_url:
            return await super().download_audio(song, quality="standard")
        return None

    async def fetch_lyrics(self, song: Song) -> Song:
        """Fetch LRC lyrics from Kuwo's lyrics endpoint.

        Args:
            song: Kuwo song whose lyrics should be fetched.

        Returns:
            The song with LRC lyrics populated when Kuwo provides them.
        """
        if song.lyrics or not song.id:
            return song

        try:
            result = await self._request(
                f"https://www.kuwo.cn/openapi/v1/www/lyric/getlyric?"
                f"{urlencode({'musicId': song.id})}"
            )
        except Exception as exc:
            logger.warning(f"Kuwo lyrics request failed: {exc}")
            return song

        lyric_data = result.get("data") if isinstance(result, dict) else None
        lyric_lines = (
            lyric_data.get("lrclist") if isinstance(lyric_data, dict) else None
        )
        if not isinstance(lyric_lines, list):
            return song

        lrc: list[str] = []
        for item in lyric_lines:
            if not isinstance(item, dict):
                continue
            text = item.get("lineLyric")
            time_value = item.get("time")
            if not isinstance(time_value, (int, float, str)) or isinstance(
                time_value, bool
            ):
                continue
            try:
                seconds = float(time_value)
            except (TypeError, ValueError):
                continue
            if not isinstance(text, str) or not text.strip() or seconds < 0:
                continue

            minutes, remainder = divmod(int(seconds * 100), 6000)
            lrc.append(f"[{minutes:02d}:{remainder / 100:05.2f}]{text}")

        if lrc:
            song.lyrics = "\n".join(lrc)
        return song

    async def fetch_comments(self, song: Song) -> Song:
        """Fetch popular comments from Kuwo's comment endpoint.

        Args:
            song: Kuwo song whose popular comments should be fetched.

        Returns:
            The song with comments populated when Kuwo provides them.
        """
        if song.comments or not song.id:
            return song

        params = {
            "f": "web",
            "type": "get_rec_comment",
            "isadd": 1,
            "aapiver": 1,
            "uid": "",
            "digest": 15,
            "sid": song.id,
            "start": 0,
            "count": 20,
        }
        try:
            result = await self._request(
                f"http://ncomment.kuwo.cn/com.s?{urlencode(params)}"
            )
        except Exception as exc:
            logger.warning(f"Kuwo comments request failed: {exc}")
            return song

        comment_list = result.get("hot_comments") if isinstance(result, dict) else None
        if isinstance(comment_list, list):
            song.comments = [
                {
                    "content": item.get("msg", ""),
                    "nickname": item.get("u_name", ""),
                    "likedCount": item.get("like_num", 0),
                }
                for item in comment_list
                if isinstance(item, dict) and item.get("msg")
            ] or None
        return song
