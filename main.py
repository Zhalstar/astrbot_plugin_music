import shlex
import traceback

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.session_waiter import (
    SessionController,
    session_waiter,
)

from .core.config import PluginConfig
from .core.lyrics_renderer import LyricsRenderer
from .core.music_service import MusicService
from .core.platform import BaseMusicPlayer
from .core.sender import MusicSender
from .core.song_renderer import CardRenderer
from .core.utils import parse_user_input


class MusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.cfg = PluginConfig(config, context)
        self.lyrics_renderer = LyricsRenderer(self.cfg)
        self.song_renderer = CardRenderer(self.cfg)
        self.music_service = MusicService(self.cfg)
        self.sender = MusicSender(
            self.cfg,
            self.context,
            self.lyrics_renderer,
            self.song_renderer,
            self.music_service,
        )
        self.trigger_routes: dict[str, tuple[BaseMusicPlayer, str | None]] = {}

    async def initialize(self):
        """Initialize music sources and build chat command routes."""
        await self.music_service.initialize()
        self.trigger_routes.clear()
        for player in self.music_service.players:
            for trigger in player.platform_cfg.trigger_words or []:
                command, separator, source = trigger.partition(":")
                command = command.strip().lower()
                if not command:
                    continue
                self.trigger_routes[command] = (
                    player,
                    source.strip() if separator and source.strip() else None,
                )
        logger.debug(
            f"Registered music trigger words: {['点歌', *self.trigger_routes]}"
        )

    @filter.on_platform_loaded()
    async def _on_platform_loaded(self) -> None:
        """Register QQ interaction handling before the platform starts."""
        self.sender.set_interaction_create()

    async def terminate(self):
        """Close plugin resources and unload the music service."""
        try:
            await self.sender.close()
        finally:
            await self.music_service.close()
            self.trigger_routes.clear()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录Q音", alias={"登录QQ音乐"})
    async def login_qqmusic(self, event: AstrMessageEvent):

        async def on_qr(image: bytes) -> None:
            await event.send(
                MessageChain([Plain("用QQ扫码登录:"), Image.fromBytes(image)])
            )

        async def on_message(message: str) -> None:
            await event.send(event.plain_result(message))

        try:
            await self.music_service.start_login("qqmusic", on_qr, on_message)
        finally:
            event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录酷狗")
    async def login_kugou(self, event: AstrMessageEvent):
        async def on_qr(image: bytes) -> None:
            await event.send(
                MessageChain([Plain("用酷狗扫码登录:"), Image.fromBytes(image)])
            )

        async def on_message(message: str) -> None:
            await event.send(event.plain_result(message))

        try:
            await self.music_service.start_login("kugou", on_qr, on_message)
        finally:
            event.stop_event()

    @filter.command("点歌")
    async def search_song(self, event: AstrMessageEvent):
        """点歌 <歌手歌名>, 也可用命令：网易点歌 NJ点歌 QQ点歌..."""
        # 此函数仅用于注册显示命令
        pass

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_search_song(self, event: AstrMessageEvent):
        """根据音源触发词路由歌曲搜索。"""
        if not event.is_at_or_wake_command:
            return
        cmd, _, arg = event.message_str.partition(" ")
        if not arg:
            return
        cmd = cmd.lower()
        if cmd != "点歌" and cmd not in self.trigger_routes:
            return
        is_generic_search = cmd == "点歌"
        route = None if is_generic_search else self.trigger_routes.get(cmd)
        player = route[0] if route else None
        source = route[1] if route else None
        if not self.music_service.players or (not is_generic_search and player is None):
            return
        args = arg.split()
        index: int = int(args[-1]) if args[-1].isdigit() else 0
        song_name = arg.removesuffix(str(index))
        if not song_name:
            yield event.plain_result("未指定歌名")
            return
        result = await self.music_service.search_songs(
            song_name,
            platform=player.platform.name if player else "",
            sub_source=source,
            limit=self.cfg.real_song_limit,
            fallback=is_generic_search,
            require_match=is_generic_search,
        )
        songs = result.songs
        player = result.player
        if not songs or player is None:
            attempted = "、".join(result.attempted_sources) or "无可用音源"
            yield event.plain_result(
                f"未找到可靠匹配歌曲：{song_name}\n已尝试音源：{attempted}"
            )
            return
        selected_player = player

        # 单曲模式
        if len(songs) == 1:
            index = 1

        # 输入了序号，直接发送歌曲
        if index and 0 <= index <= len(songs):
            selected_song = songs[int(index) - 1]
            await self.sender.send_song(event, selected_player, selected_song)
            event.stop_event()
            return

        # 未提输入序号，等待用户选择歌曲
        selection_mode = await self.sender.send_song_selection(
            event=event, songs=songs, player=selected_player
        )

        if selection_mode == "button":
            event.stop_event()
            return
        if selection_mode not in {"image", "text"}:
            self.sender.clear_selection_context(event)
            event.stop_event()
            return

        @session_waiter(timeout=self.cfg.search_config.timeout)
        async def empty_mention_waiter(
            controller: SessionController, event: AstrMessageEvent
        ):
            arg = event.message_str.strip()
            arg_lower = arg.lower()
            waiter_command = arg_lower.partition(" ")[0]
            if waiter_command == "点歌" or waiter_command in self.trigger_routes:
                controller.stop()
                return
            # 解析输入格式
            index, modes, error = parse_user_input(arg)
            if error:
                await event.send(event.plain_result(error))
                return
            if index == 0:
                return
            if index < 1 or index > len(songs):
                controller.stop()
                return
            selected_song = songs[index - 1]
            controller.stop()
            await self.sender.send_song(
                event, selected_player, selected_song, modes=modes
            )

        try:
            await empty_mention_waiter(event)
        except TimeoutError as _:
            self.sender.clear_selection_context(event)
            yield event.plain_result("点歌超时！")
        except Exception as e:
            logger.error(traceback.format_exc())
            logger.error("点歌发生错误" + str(e))

        event.stop_event()

    @filter.command("查歌词", alias={"查看歌词"})
    async def query_lyrics(self, event: AstrMessageEvent, song_name: str):
        """查歌词 <歌名> [-歌手] [-音源]"""
        try:
            args = shlex.split(song_name)
        except ValueError:
            yield event.plain_result(
                "参数格式无效，请使用：查歌词 歌名 [-歌手] [-音源]"
            )
            return

        artist_parts = []
        requested_platform = ""
        while args and args[-1].startswith("-"):
            option = args.pop()[1:]
            if not option:
                continue
            if self.music_service.get_player(option):
                requested_platform = option
            else:
                artist_parts.insert(0, option)

        query = " ".join(args).strip()
        if not query:
            yield event.plain_result("请提供要查询的歌名")
            return
        if requested_platform and not self.music_service.get_player(requested_platform):
            yield event.plain_result(f"音源“{requested_platform}”不可用")
            return
        result = await self.music_service.find_lyrics(
            query, artist=" ".join(artist_parts), platform=requested_platform
        )
        if result.player is not None and result.best_song is not None:
            await self.sender.send_lyrics(event, result.player, result.best_song)
            return
        yield event.plain_result("未找到匹配的歌曲或歌词")

    @filter.llm_tool()
    async def query_lyrics_by_name(
        self,
        event: AstrMessageEvent,
        song_name: str,
        platform: str = "",
        artist: str = "",
    ):
        """根据歌名搜索歌词，可指定歌手和优先音源。

        Args:
            song_name(string): 歌曲名称或搜索关键词，可包含歌手名。
            platform(string): 优先使用的音源标识或显示名称；未找到匹配歌曲或歌词时，
                会按音源优先级尝试其他已启用音源。
            artist(string): 可选歌手名；填写后只接受歌手匹配的搜索结果。
        """
        requested_platform = platform.strip()
        if requested_platform and not self.music_service.get_player(requested_platform):
            return f"音源“{requested_platform}”不可用"
        result = await self.music_service.find_lyrics(
            song_name, artist=artist, platform=requested_platform
        )
        player, song = result.player, result.best_song
        if player is not None and song is not None:
            await self.sender.send_lyrics(event, player, song)
            return (
                f"音源：{player.platform.display_name}({player.platform.name})\n "
                f"歌曲：{song.name}\n"
                f"作者：{song.artists}\n"
                f"歌词：\n{song.lyrics}"
            )
        return "未找到匹配的歌曲或歌词"

    @filter.llm_tool()
    async def play_song_by_name(
        self,
        event: AstrMessageEvent,
        song_name: str,
        platform: str = "",
        artist: str = "",
        send_mode: str = "",
    ):
        """当用户想听歌时，根据歌名（可含歌手）搜索并播放音乐。

        Args:
            song_name(string): 歌曲名称或包含歌手的关键词
            platform(string): 音源标识或显示名称，例如 netease、netease_api、kuwo、qqmusic、kugou、kugou_api、soda、txqq、neko、apple。指定后仅使用该音源；留空时按音源优先级回退。
            artist(string): 可选的指定歌手。填写后只接受歌手匹配的结果。
            send_mode(string): 可选的本次发送方式，支持 native_card、ark_card、record_link、record_local、file_link、file_local、text, 默认留空使用用户自定义顺序。
        """
        if send_mode and send_mode not in {
            "native_card",
            "ark_card",
            "record_link",
            "record_local",
            "file_link",
            "file_local",
            "text",
        }:
            return f"不支持的发送方式：{send_mode}"
        send_modes = [send_mode] if send_mode else self.cfg.real_send_modes
        if platform and not self.music_service.get_player(platform):
            return f"音源「{platform}」不可用；指定音源时不会自动回退"
        result = await self.music_service.search_songs(
            song_name, artist=artist, platform=platform, fallback=False
        )
        selected_song, selected_player = result.best_song, result.player

        if not selected_song or not selected_player:
            attempted = "、".join(result.attempted_sources) or "无可用音源"
            return (
                f"未找到与《{song_name}》"
                f"{f'（{artist}演唱）' if artist else ''}可靠匹配的歌曲；"
                f"已尝试音源：{attempted}"
            )

        platform_name = f"{selected_player.platform.display_name}（{selected_player.platform.name}）"
        sent, comment_sent = await self.sender.send_song(
            event, selected_player, selected_song, modes=send_modes
        )
        if not sent:
            return f"音源「{platform_name}」发送《{selected_song.name}》失败"
        if self.cfg.send_config.return_play_song_result and not comment_sent:
            author = selected_song.artists or "未知歌手"
            return (
                f"已通过音源「{platform_name}」发送《{selected_song.name} - {author}》"
            )
        event.stop_event()
