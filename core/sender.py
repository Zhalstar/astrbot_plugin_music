import asyncio
import base64
import json
import random
import re
import time
import uuid
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image as PILImage

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.components import File, Image, Plain, Record
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
    QQOfficialMessageEvent,
)
from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
    QQOfficialPlatformAdapter,
)
from astrbot.core.star.context import Context

from .ark_card import ArkCard
from .config import PluginConfig
from .lyrics_renderer import LyricsRenderer
from .model import Song
from .music_service import MusicService
from .platform import BaseMusicPlayer
from .song_renderer import CardRenderer

UNICODE_ESCAPE_PATTERN = re.compile(r"\\u([0-9a-fA-F]{4})")
COMMENT_NOISE_PATTERN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\ue000-\uf8ff]")
COMMENT_EMOJI_PATTERN = re.compile(r"\[[a-zA-Z]+\]e\d+\[/[a-zA-Z]+\]|(?:e\d{6})+")


def clean_comment_text(content: str) -> str:
    """还原转义序列并清理评论里的控制符与私用区字符。"""
    content = UNICODE_ESCAPE_PATTERN.sub(lambda m: chr(int(m.group(1), 16)), content)
    # 合并代理对，丢弃孤立代理项，避免残留半截字符
    content = content.encode("utf-16", "surrogatepass").decode("utf-16", "ignore")
    content = COMMENT_NOISE_PATTERN.sub("", content)
    content = COMMENT_EMOJI_PATTERN.sub("", content)
    return re.sub(r"\[[^\]]*]", "", content).replace("\\n", "\n").strip()


if TYPE_CHECKING:
    from botpy.interaction import Interaction

_SELECTION_TTL_SECONDS = 600.0
_MAX_SELECTION_ENTRIES = 256
_COVER_CONCURRENCY = 5


class MusicSender:
    def __init__(
        self,
        config: PluginConfig,
        context: Context,
        lyrics_renderer: LyricsRenderer,
        song_renderer: CardRenderer,
        music_service: MusicService,
    ):
        """Create message presentation using the plugin's shared music service.

        Args:
            config: Plugin configuration.
            context: AstrBot context used for platform interactions.
            lyrics_renderer: Renderer for lyric images.
            song_renderer: Renderer for candidate menus.
            music_service: Service owned and managed by the plugin lifecycle.
        """
        self.cfg = config
        self.context = context
        self.lyrics_renderer = lyrics_renderer
        self.song_renderer = song_renderer
        self.music_service = music_service
        session_factory = (
            music_service.get_http_session
            if isinstance(music_service, MusicService)
            else None
        )
        self.ark_card = ArkCard(config, session_factory=session_factory)
        self._selection_message_ids: dict[str, str | int] = {}
        self._selection_message_timestamps: dict[str, float] = {}
        self._selection_contexts: dict[str, dict[str, Any]] = {}
        self._selection_context_ids: dict[str, str] = {}
        self._interaction_clients: set[int] = set()

    async def close(self) -> None:
        """Release resources owned by the sender."""
        self._selection_message_ids.clear()
        self._selection_message_timestamps.clear()
        self._selection_contexts.clear()
        self._selection_context_ids.clear()
        await self.ark_card.close()

    def _unlink_temp_audio(self, file_path: Path) -> None:
        """Delete a failed download only if it resides in the plugin temp directory."""
        try:
            if file_path.parent.resolve() == self.cfg.temp_dir.resolve():
                file_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug(f"Failed to remove temp audio file {file_path}: {exc}")

    def set_interaction_create(self):
        """Register interaction intents and handlers for current QQ clients."""
        for platform in self.context.platform_manager.get_insts():
            if not isinstance(platform, QQOfficialPlatformAdapter):
                continue
            client = platform.get_client()
            client_id = id(client)
            if client_id in self._interaction_clients:
                continue
            intents = getattr(client, "intents", None)
            if isinstance(intents, int):
                client.intents = intents | (1 << 25) | (1 << 26)
            elif intents is not None:
                intents.interaction = True
            platform_intents = getattr(platform, "intents", None)
            if platform_intents is not None:
                platform_intents.interaction = True

            previous_handler = getattr(client, "on_interaction_create", None)

            async def on_interaction_create(
                interaction: "Interaction",
                previous_handler=previous_handler,
            ):
                if await self.handle_interaction(interaction):
                    return
                if previous_handler is not None:
                    await previous_handler(interaction)

            setattr(client, "on_interaction_create", on_interaction_create)
            self._interaction_clients.add(client_id)

    @staticmethod
    def _format_time(duration_ms):
        duration = duration_ms // 1000

        hours = duration // 3600
        minutes = (duration % 3600) // 60
        seconds = duration % 60

        if hours > 0:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        else:
            return f"{minutes:02d}:{seconds:02d}"

    async def _recall_qqofficial_message(
        self, event: QQOfficialMessageEvent, message_id: str
    ) -> None:
        import botpy.message
        from botpy.http import Route

        source = event.message_obj.raw_message
        route_path = None
        route_params = {}

        if isinstance(source, botpy.message.GroupMessage):
            route_path = "/v2/groups/{group_openid}/messages/{message_id}"
            route_params["group_openid"] = source.group_openid
        elif isinstance(source, botpy.message.C2CMessage):
            route_path = "/v2/users/{openid}/messages/{message_id}"
            route_params["openid"] = source.author.user_openid
        elif isinstance(source, botpy.message.DirectMessage):
            route_path = "/dms/{guild_id}/messages/{message_id}"
            route_params["guild_id"] = source.guild_id
        elif isinstance(source, botpy.message.Message):
            await event.bot.api.recall_message(
                channel_id=source.channel_id,
                message_id=message_id,
            )
            return

        if route_path:
            await event.bot.api._http.request(
                Route(
                    "DELETE",
                    route_path,
                    message_id=message_id,
                    **route_params,
                )
            )

    @staticmethod
    def _make_selection_key(event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"

    @staticmethod
    def _prune_ttl_entries(
        entries: dict[str, Any],
        get_ts: Callable[[str, Any], float],
        now: float,
    ) -> list[str]:
        """Evict expired or excess entries in insertion order and return removed keys."""
        removed = [
            key
            for key, value in entries.items()
            if now - get_ts(key, value) >= _SELECTION_TTL_SECONDS
        ]
        for key in removed:
            entries.pop(key, None)
        while len(entries) > _MAX_SELECTION_ENTRIES:
            oldest_key = next(iter(entries))
            entries.pop(oldest_key, None)
            removed.append(oldest_key)
        return removed

    def _prune_selection_state(self, now: float | None = None) -> None:
        """Evict expired or excess selection contexts and message IDs."""
        current = time.monotonic() if now is None else now
        self._prune_ttl_entries(
            self._selection_contexts,
            lambda _k, ctx: float(ctx.get("created_at", current)),
            current,
        )
        stale_keys = [
            key
            for key, sel_id in self._selection_context_ids.items()
            if sel_id not in self._selection_contexts
        ]
        for key in stale_keys:
            self._selection_context_ids.pop(key, None)

        removed_msg_keys = self._prune_ttl_entries(
            self._selection_message_ids,
            lambda key, _val: self._selection_message_timestamps.get(key, current),
            current,
        )
        for key in removed_msg_keys:
            self._selection_message_timestamps.pop(key, None)

    def _record_selection_message(
        self, event: AstrMessageEvent, message_id: str | int
    ) -> None:
        now = time.monotonic()
        self._prune_selection_state(now)
        key = self._make_selection_key(event)
        self._selection_message_ids[key] = message_id
        self._selection_message_timestamps[key] = now

    def clear_selection_context(self, event: AstrMessageEvent) -> None:
        """Remove the active selection context for an event.

        Args:
            event: The event whose song selection has finished.
        """
        selection_key = self._make_selection_key(event)
        selection_id = self._selection_context_ids.pop(selection_key, None)
        if selection_id:
            self._selection_contexts.pop(selection_id, None)

    async def _recall_selection_message(self, event: AstrMessageEvent) -> None:
        key = self._make_selection_key(event)
        self._selection_message_timestamps.pop(key, None)
        message_id = self._selection_message_ids.pop(key, None)
        if message_id is None:
            return

        try:
            if isinstance(event, AiocqhttpMessageEvent):
                await event.bot.delete_msg(message_id=int(message_id))
            elif isinstance(event, QQOfficialMessageEvent):
                await self._recall_qqofficial_message(event, str(message_id))
        except Exception as e:
            logger.warning(f"Failed to recall song selection message: {e}")

    async def handle_interaction(self, interaction: Any) -> bool:
        resolved = getattr(getattr(interaction, "data", None), "resolved", None)
        if not getattr(resolved, "button_id", "").startswith("music_song_selection_"):
            return False

        self._prune_selection_state()
        button_data = getattr(resolved, "button_data", "") or ""
        try:
            payload = json.loads(button_data)
        except (json.JSONDecodeError, TypeError):
            return True
        if not isinstance(payload, dict):
            return True

        selection_id = str(payload.get("selection_id") or "")
        try:
            index = int(payload.get("index"))  # type: ignore
        except (TypeError, ValueError):
            return True
        context = self._selection_contexts.get(selection_id)
        # 3 = 重复操作：选歌已处理或已过期
        if context is None or context.get("handled"):
            await interaction._api.on_interaction_result(interaction.id, 3)
            return True

        event = context["event"]
        selection_key = self._make_selection_key(event)
        if self._selection_context_ids.get(selection_key) != selection_id:
            await interaction._api.on_interaction_result(interaction.id, 3)
            return True

        context["handled"] = True
        songs = context["songs"]
        if index < 1 or index > len(songs):
            return True
        self.clear_selection_context(event)
        await interaction._api.on_interaction_result(interaction.id, 0)
        asyncio.create_task(
            self.send_song(
                event,
                context["player"],
                songs[index - 1],
            )
        )
        return True

    @staticmethod
    def _decode_cover(
        data: bytes, target_size: tuple[int, int]
    ) -> PILImage.Image:
        with PILImage.open(BytesIO(data)) as raw_image:
            image = raw_image.convert("RGB")
            if image.size != target_size:
                image = image.resize(target_size)
            return image

    async def _build_cover_map(
        self, player: BaseMusicPlayer, cover_urls: list[str]
    ) -> dict[str, PILImage.Image]:
        """Download and decode candidate covers concurrently using their source adapter.

        Args:
            player: Adapter responsible for downloading the covers.
            cover_urls: Cover image URLs to decode.

        Returns:
            Successfully decoded covers keyed by their original URL.
        """
        unique_urls = list(dict.fromkeys(url for url in cover_urls if url))
        if not unique_urls:
            return {}

        theme = self.song_renderer.theme
        target_size = (theme.card_width, theme.thumb_height)
        semaphore = asyncio.Semaphore(_COVER_CONCURRENCY)

        async def _fetch_and_decode(
            cover_url: str,
        ) -> tuple[str, PILImage.Image | None]:
            async with semaphore:
                try:
                    data = await player.download_cover(cover_url)
                    if not data:
                        return cover_url, None
                    image = await asyncio.to_thread(
                        self._decode_cover, data, target_size
                    )
                    return cover_url, image
                except Exception as e:
                    logger.warning(f"封面下载失败: {cover_url}, {e}")
                    return cover_url, None

        results = await asyncio.gather(
            *(_fetch_and_decode(url) for url in unique_urls)
        )
        return {url: image for url, image in results if image is not None}

    @staticmethod
    async def send_msg(event: AiocqhttpMessageEvent, payloads: dict) -> int | None:
        if event.is_private_chat():
            payloads["user_id"] = event.get_sender_id()
            result = await event.bot.api.call_action("send_private_msg", **payloads)
        else:
            payloads["group_id"] = event.get_group_id()
            result = await event.bot.api.call_action("send_group_msg", **payloads)
        return result.get("message_id")

    async def send_selection(self, event: AstrMessageEvent, text: str) -> None:
        """Send and track a text selection message for later recall.

        Args:
            event: Message event receiving the selection.
            text: Complete selection prompt and candidate list.
        """
        message_id: str | int | None = None
        if isinstance(event, AiocqhttpMessageEvent):
            payloads = {"message": [{"type": "text", "data": {"text": text}}]}
            message_id = await self.send_msg(event, payloads)
        elif isinstance(event, QQOfficialMessageEvent):
            platform = getattr(event.bot, "platform", None)
            if platform is not None:
                await platform.send_by_session(
                    event.session,
                    MessageChain(chain=[Plain(text=text)]),
                )
                message_id = getattr(platform, "_session_last_message_id", {}).get(
                    event.session_id
                )
            else:
                await event.send(event.plain_result(text))
        else:
            await event.send(event.plain_result(text))

        if message_id is not None:
            self._record_selection_message(event, message_id)

    async def _send_song_selection_text(
        self,
        event: AstrMessageEvent,
        songs: list[Song],
        player: BaseMusicPlayer,
    ) -> None:
        lins = [f"【{player.platform.display_name}】"]
        for index, song in enumerate(songs):
            lins.append(f"{index + 1}. {song.name} - {song.artists or ''}")
        msg = "\n".join(lins)
        await self.send_selection(event, msg)

    async def _send_song_selection_button(
        self,
        event: QQOfficialMessageEvent,
        songs: list[Song],
        player: BaseMusicPlayer,
    ) -> str | None:
        import botpy.message
        from botpy.types.inline import (
            Action,
            Button,
            Keyboard,
            KeyboardRow,
            Permission,
            RenderData,
        )
        from botpy.types.message import KeyboardPayload, MarkdownPayload

        self.set_interaction_create()
        now = time.monotonic()
        self._prune_selection_state(now)
        selection_key = self._make_selection_key(event)
        previous_id = self._selection_context_ids.pop(selection_key, None)
        if previous_id:
            self._selection_contexts.pop(previous_id, None)

        selection_id = uuid.uuid4().hex
        selection_context = {
            "event": event,
            "songs": songs,
            "player": player,
            "handled": False,
            "created_at": now,
        }
        self._selection_contexts[selection_id] = selection_context
        self._selection_context_ids[selection_key] = selection_id

        buttons = []
        for index, song in enumerate(songs, 1):
            song_info = f"{song.name} - {song.artists}"
            button_data = json.dumps(
                {"selection_id": selection_id, "index": index},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            buttons.append(
                Button(
                    id=f"music_song_selection_{index}",
                    render_data=RenderData(
                        label=f"{index}. {song_info}",
                        visited_label=f"正在发送“{song_info}”...",
                        style=1,
                    ),
                    action=Action(
                        type=1,
                        permission=Permission(
                            type=0,
                            specify_role_ids=[],
                            specify_user_ids=[event.get_sender_id()],
                        ),
                        click_limit=1,
                        data=button_data,
                        at_bot_show_channel_list=False,
                    ),
                )
            )

        keyboard = Keyboard(rows=[KeyboardRow(buttons=[button]) for button in buttons])
        title = f"【{player.platform.display_name}】"
        markdown = MarkdownPayload(content=f"### {title}请选歌：")
        keyboard_payload = KeyboardPayload(content=keyboard)
        source = event.message_obj.raw_message
        result = None

        if isinstance(source, botpy.message.GroupMessage):
            result = await event.bot.api.post_group_message(
                group_openid=source.group_openid,  # type: ignore
                msg_type=2,
                markdown=markdown,
                keyboard=keyboard_payload,  # type: ignore[arg-type]
                msg_id=source.id,
            )
        elif isinstance(source, botpy.message.C2CMessage):
            result = await event.post_c2c_message(
                openid=source.author.user_openid,
                msg_type=2,
                markdown=markdown,
                keyboard=keyboard_payload,  # type: ignore[arg-type]
                msg_id=source.id,
            )
        elif isinstance(source, botpy.message.DirectMessage):
            result = await event.bot.api.post_dms(
                guild_id=source.guild_id,
                markdown=markdown,
                keyboard=keyboard_payload,  # type: ignore[arg-type]
                msg_id=source.id,
            )
        elif isinstance(source, botpy.message.Message):
            result = await event.bot.api.post_message(
                channel_id=source.channel_id,
                markdown=markdown,
                keyboard=keyboard_payload,  # type: ignore[arg-type]
                msg_id=source.id,
            )
        else:
            raise ValueError(f"Unsupported QQ message type: {type(source).__name__}")

        if result is None:
            raise RuntimeError("QQ API returned no message for song selection")
        message_id = (
            result.get("id")
            if isinstance(result, dict)
            else getattr(result, "id", None)
        )
        if message_id is None:
            raise RuntimeError("QQ API response has no message ID")
        self._record_selection_message(event, message_id)
        return str(message_id)

    async def _send_song_selection_image(
        self,
        event: AstrMessageEvent,
        songs: list[Song],
        player: BaseMusicPlayer | None = None,
    ) -> str | int | None:
        song_items = list(songs)
        if player is not None:
            missing_cover_indices = [
                idx for idx, song in enumerate(song_items) if not song.cover_url
            ]
            if missing_cover_indices:
                semaphore = asyncio.Semaphore(_COVER_CONCURRENCY)

                async def _fetch_cover_meta(idx: int) -> tuple[int, Song]:
                    original = song_items[idx]
                    async with semaphore:
                        try:
                            return idx, await player.fetch_extra(original)
                        except Exception as exc:
                            logger.warning(
                                f"获取候选歌曲封面失败: {original.name}, {exc}"
                            )
                            return idx, original

                fetched = await asyncio.gather(
                    *(_fetch_cover_meta(idx) for idx in missing_cover_indices)
                )
                for idx, enriched in fetched:
                    song_items[idx] = enriched

        cover_urls = [song.cover_url for song in song_items if song.cover_url]

        cover_map = await self._build_cover_map(player, cover_urls) if player else {}
        image_bytes = await self.song_renderer.render_song_list_image(
            song_items, cover_map
        )

        if isinstance(event, AiocqhttpMessageEvent):
            payloads = {
                "message": [
                    {
                        "type": "image",
                        "data": {
                            "file": f"base64://{base64.b64encode(image_bytes).decode()}",
                        },
                    }
                ]
            }
            message_id = await self.send_msg(event, payloads)
        elif isinstance(event, QQOfficialMessageEvent):
            platform = getattr(event.bot, "platform", None)
            if platform is not None:
                await platform.send_by_session(
                    event.session,
                    MessageChain(chain=[Image.fromBytes(image_bytes)]),
                )
                message_id = getattr(platform, "_session_last_message_id", {}).get(
                    event.session_id
                )
            else:
                await event.send(MessageChain(chain=[Image.fromBytes(image_bytes)]))
                message_id = None
        else:
            await event.send(MessageChain(chain=[Image.fromBytes(image_bytes)]))
            message_id = None

        if message_id is not None:
            self._record_selection_message(event, message_id)
        return message_id

    async def send_song_selection(
        self,
        event: AstrMessageEvent,
        songs: list[Song],
        player: BaseMusicPlayer,
    ) -> str | None:
        for mode in self.cfg.real_select_modes:
            try:
                if mode == "button":
                    if not isinstance(event, QQOfficialMessageEvent):
                        continue
                    await self._send_song_selection_button(event, songs, player)
                    return mode
                if mode == "image":
                    await self._send_song_selection_image(
                        event=event, songs=songs, player=player
                    )
                    return mode
                if mode == "text":
                    await self._send_song_selection_text(event, songs, player)
                    return mode
                if mode != "single":
                    logger.warning(f"Unknown song selection mode: {mode}")
            except Exception as e:
                logger.warning(f"Song selection mode '{mode}' failed: {e}")

        logger.error("All configured song selection modes failed")
        return None

    async def _send_card(
        self, event: AiocqhttpMessageEvent, player: BaseMusicPlayer, song: Song
    ) -> bool:
        """发卡片"""
        card_type = song.card_type
        # 原生卡片实测支持性太差，故注释掉
        if not song.audio_url:
            logger.warning(f"Cannot send a music card without audio URL: {song.source}")
            return False
        music_data = {
            "type": card_type or "custom",
            "url": song.page_url or song.audio_url,
            "audio": song.audio_url,
            "title": song.name or "",
            "content": song.artists or "",
            "image": song.cover_url or "",
        }

        payloads = {"message": [{"type": "music", "data": music_data}]}
        try:
            await self.send_msg(event, payloads)
            return True
        except Exception as e:
            logger.warning(f"Native music card send failed: {e}")
            return False

    async def _send_ark_card(
        self, event: AiocqhttpMessageEvent, player: BaseMusicPlayer, song: Song
    ) -> bool:
        """发送签名音乐卡片。"""
        ark = await self.ark_card.fetch(player, song)
        if not ark:
            return False
        data = json.dumps(ark, ensure_ascii=False)
        payloads = {"message": [{"type": "json", "data": {"data": data}}]}
        try:
            await self.send_msg(event, payloads)
            return True
        except Exception as exc:
            logger.warning(f"Ark music card send failed: {type(exc).__name__}")
            return False

    async def _send_record_link(self, event: AstrMessageEvent, audio_url: str) -> bool:
        try:
            if (
                isinstance(event, AiocqhttpMessageEvent)
                and self.cfg.send_config.onebot_direct_audio is not False
            ):
                payloads = {
                    "message": [{"type": "record", "data": {"file": audio_url}}]
                }
                return await self.send_msg(event, payloads) is not None
            seg = Record.fromURL(audio_url)
            await event.send(event.chain_result([seg]))
            return True
        except Exception as e:
            logger.error(f"Record link send failed: {e}")
            return False

    async def _send_record_local(
        self, event: AstrMessageEvent, file_path: Path
    ) -> bool:
        try:
            if (
                isinstance(event, AiocqhttpMessageEvent)
                and self.cfg.send_config.onebot_direct_audio is not False
            ):
                # OneBot must be able to access this path, such as through a shared mount.
                payloads = {
                    "message": [
                        {
                            "type": "record",
                            "data": {"file": file_path.resolve().as_uri()},
                        }
                    ]
                }
                return await self.send_msg(event, payloads) is not None
            seg = Record.fromFileSystem(str(file_path.resolve()))
            await event.send(event.chain_result([seg]))
            return True
        except Exception as e:
            logger.error(f"Local voice send failed: {e}")
            return False

    async def _send_file_link(
        self, event: AstrMessageEvent, audio_url: str, file_name: str
    ) -> bool:
        try:
            seg_url = File(name=file_name, url=audio_url)
            await event.send(event.chain_result([seg_url]))
            return True
        except Exception as e_url:
            logger.error(f"File link send failed: {e_url}")
            return False

    async def _send_file_local(
        self, event: AstrMessageEvent, file_path: Path, file_name: str
    ) -> bool:
        try:
            seg = File(name=file_name, file=str(file_path.resolve()))
            await event.send(event.chain_result([seg]))
            return True
        except Exception as e:
            logger.error(f"Local file send failed: {e}")
            return False

    async def _send_text(self, event: AstrMessageEvent, audio_url: str) -> bool:
        """发文本"""
        try:
            await event.send(event.plain_result(audio_url))
            return True
        except Exception as e:
            logger.error(f"文本模式发送歌曲失败: {e}")
            return False

    async def send_comment(
        self, event: AstrMessageEvent, player: BaseMusicPlayer, song: Song
    ) -> bool:
        """Fetch popular comments through the service and send a random comment.

        Args:
            event: Message event receiving the comment.
            player: Source that provided the selected song.
            song: Song whose popular comment should be sent.

        Returns:
            Whether a comment was successfully sent.
        """
        song = await self.music_service.fetch_comments(player, song)
        if not song.comments:
            return False
        try:
            content = clean_comment_text(
                random.choice(song.comments).get("content", "")
            )
            if not content:
                return False
            await event.send(event.plain_result(content))
            return True
        except Exception:
            return False

    async def send_lyrics(
        self, event: AstrMessageEvent, player: BaseMusicPlayer, song: Song
    ) -> bool:
        """发歌词"""
        if not song.lyrics:
            await player.fetch_lyrics(song)
        if song.lyrics:
            await player.resolve_lyrics(song)
        if not song.lyrics:
            logger.error(f"【{song.name}】歌词获取失败")
            return False
        try:
            image = await asyncio.to_thread(
                self.lyrics_renderer.draw_lyrics, song.lyrics
            )
            await event.send(MessageChain(chain=[Image.fromBytes(image)]))
            return True
        except Exception as e:
            logger.error(f"【{song.name}】歌词渲染/发送失败: {e}")
            return False

    def _get_sender(self, mode: str):
        return {
            "native_card": self._send_card,
            "ark_card": self._send_ark_card,
        }.get(mode)

    def _is_mode_supported(self, mode: str, event: AstrMessageEvent) -> bool:
        platform = event.get_platform_name()
        match mode:
            case "text":
                return True
            case "native_card":
                return platform == "aiocqhttp"
            case "ark_card":
                return platform == "aiocqhttp"
            case "record_link" | "record_local":
                return platform not in self.cfg.send_config.record_unsupported
            case "file_link" | "file_local":
                return platform not in self.cfg.send_config.file_unsupported
            case _:
                return False

    async def send_song(
        self,
        event: AstrMessageEvent,
        player: BaseMusicPlayer,
        song: Song,
        modes: list[str] | None = None,
    ) -> tuple[bool, bool]:
        target_modes = modes or self.cfg.real_send_modes
        needs_audio = any(
            m not in ("native_card", "ark_card", "text") for m in target_modes
        )

        if not song.audio_url or not song.cover_url:
            song = await player.fetch_extra(song)

        if (
            needs_audio
            and not song.audio_url
            and target_modes
            and target_modes[0] != "native_card"
        ):
            await event.send(event.plain_result(f"【{song.name}】音频获取失败"))
            return False, False

        logger.debug(
            f"{event.get_sender_name()}（{event.get_sender_id()}）点歌："
            f"{player.platform.display_name} -> {song.name}_{song.artists}"
            f"音频链接：{song.audio_url}, 封面链接：{song.cover_url}"
        )
        sent = False
        target_modes = modes if modes is not None else self.cfg.real_send_modes

        for mode in target_modes:
            if mode == "native_card" and not song.card_type:
                logger.debug(
                    f"{player.platform.name} does not support card sending; skipping"
                )
                continue
            if mode == "ark_card" and not (self.cfg.send_config.ark_key or "").strip():
                logger.debug("Ark card API key is not configured; skipping")
                continue
            if not self._is_mode_supported(mode, event):
                logger.debug(f"{mode} 不支持，跳过")
                continue

            downloaded_temp_path: Path | None = None
            try:
                if mode == "record_link":
                    ok = bool(song.audio_url) and await self._send_record_link(
                        event, song.audio_url
                    )
                elif mode == "record_local":
                    downloaded_temp_path = await player.download_audio(
                        song, quality="standard"
                    )
                    if not downloaded_temp_path:
                        logger.error(f"【{song.name}】下载失败")
                        ok = False
                    else:
                        ok = await self._send_record_local(event, downloaded_temp_path)
                elif mode == "file_link":
                    ok = bool(song.audio_url) and await self._send_file_link(
                        event,
                        song.audio_url,
                        f"{song.name}_{song.artists}.mp3",
                    )
                elif mode == "file_local":
                    downloaded_temp_path = await player.download_audio(
                        song, quality=getattr(player.platform_cfg, "quality", None)
                    )
                    if not downloaded_temp_path:
                        logger.error(f"【{song.name}】下载失败")
                        ok = False
                    else:
                        ok = await self._send_file_local(
                            event,
                            downloaded_temp_path,
                            f"{song.name}_{song.artists}{downloaded_temp_path.suffix}",
                        )
                elif mode == "text":
                    ok = bool(song.audio_url) and await self._send_text(
                        event, song.audio_url
                    )
                else:
                    sender = self._get_sender(mode)
                    if not sender:
                        continue
                    ok = await sender(event, player, song)  # type: ignore
            except asyncio.CancelledError:
                if downloaded_temp_path is not None:
                    self._unlink_temp_audio(downloaded_temp_path)
                raise
            except Exception as e:
                logger.error(f"{mode} 发送异常: {e}")
                ok = False

            if not ok and downloaded_temp_path is not None:
                self._unlink_temp_audio(downloaded_temp_path)

            if ok:
                logger.debug(f"{mode} 发送成功")
                sent = True
                break
            else:
                logger.debug(f"{mode} 发送失败，尝试下一种")

        if not sent:
            await event.send(event.plain_result("歌曲发送失败"))
            return False, False

        try:
            await self.music_service.store.record_play(player.platform.name, song)
        except Exception:
            logger.exception("Failed to record music history after sending a song")

        self.clear_selection_context(event)
        if self.cfg.search_config.recall_select:
            await self._recall_selection_message(event)

        comment_sent = False
        if self.cfg.send_config.enable_comments:
            comment_sent = await self.send_comment(event, player, song)

        if self.cfg.send_config.enable_lyrics:
            await self.send_lyrics(event, player, song)

        return True, comment_sent

    async def send_audio(
        self,
        event: AstrMessageEvent,
        *,
        path: Path | None = None,
        url: str | None = None,
        name: str = "",
        artist: str = "",
    ) -> bool:
        """Send an existing audio resource through the configured fallback chain.

        Args:
            event: Message event receiving the audio.
            path: Existing local audio file. The caller retains ownership.
            url: Publicly accessible audio URL.
            name: Display name used for file messages.
            artist: Optional artist used for file messages.

        Returns:
            Whether one compatible send mode succeeded.

        Raises:
            ValueError: If neither a valid local path nor URL is provided.
        """
        file_path = Path(path).resolve() if path is not None else None
        audio_url = (url or "").strip()
        if file_path is not None and not file_path.is_file():
            raise ValueError(f"Audio file does not exist: {file_path}")
        if file_path is None and not audio_url:
            raise ValueError("A local audio path or public URL is required")

        base_name = "_".join(part for part in (name.strip(), artist.strip()) if part)
        if not base_name:
            base_name = file_path.stem if file_path is not None else "audio"
        suffix = file_path.suffix if file_path is not None else ".mp3"
        file_name = f"{base_name}{suffix}"

        for mode in self.cfg.real_send_modes:
            if not self._is_mode_supported(mode, event):
                logger.debug(f"{mode} is unsupported on this platform; skipping")
                continue
            try:
                if mode == "record_local" and file_path is not None:
                    sent = await self._send_record_local(event, file_path)
                elif mode == "record_link" and audio_url:
                    sent = await self._send_record_link(event, audio_url)
                elif mode == "file_local" and file_path is not None:
                    sent = await self._send_file_local(event, file_path, file_name)
                elif mode == "file_link" and audio_url:
                    sent = await self._send_file_link(event, audio_url, file_name)
                elif mode == "text" and audio_url:
                    sent = await self._send_text(event, audio_url)
                else:
                    continue
            except Exception as exc:
                logger.error(f"{mode} audio send failed: {exc}")
                sent = False
            if sent:
                logger.debug(f"{mode} audio send succeeded")
                if self.cfg.search_config.recall_select:
                    await self._recall_selection_message(event)
                return True

        await event.send(event.plain_result("音频发送失败"))
        return False
