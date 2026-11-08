import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, fields

from astrbot.api import logger

from .config import PluginConfig
from .model import Song
from .music_store import MusicStore
from .platform import BaseMusicPlayer
from .platform.kugou.kugou_login import KuGouLogin
from .platform.qqmusic.qqmusic_login import QQMusicLogin


@dataclass(slots=True)
class SearchResult:
    """Music search outcome shared by command, tool, and web entry points.

    Attributes:
        player: Source that provided the accepted results, if any.
        songs: Candidates from the accepted source, in their original order.
        best_song: Best reliable title and artist match, if any.
        attempted_sources: Display names of sources searched in attempt order.
    """

    player: BaseMusicPlayer | None = None
    songs: list[Song] = field(default_factory=list)
    best_song: Song | None = None
    attempted_sources: list[str] = field(default_factory=list)


class MusicService:
    """Own music sources and coordinate searches without message events."""

    def __init__(self, config: PluginConfig):
        """Create the service without opening source resources.

        Args:
            config: Plugin configuration shared with the source adapters.
        """
        self.cfg = config
        self.store = MusicStore(config.db_path)
        self.players: list[BaseMusicPlayer] = []
        self.qqmusic_login = QQMusicLogin(config)
        self.kugou_login = KuGouLogin(config)

    async def initialize(self) -> None:
        """Instantiate enabled sources once and clean up partial initialization.

        Raises:
            Exception: If a source cannot be initialized.
        """
        await self.store.initialize()
        if self.players:
            return
        enabled_platforms = set(self.cfg.players.enabled_platforms())
        try:
            for player_class in BaseMusicPlayer.get_all_subclass():
                if player_class.platform.name in enabled_platforms:
                    self.players.append(player_class(self.cfg))
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        """Cancel login sessions and close sources, continuing after failures."""
        for name, login in (
            ("qqmusic", self.qqmusic_login),
            ("kugou", self.kugou_login),
        ):
            try:
                await login.close()
            except Exception:
                logger.exception(f"Failed to close music login {name}")
        for player in self.players:
            try:
                await player.close()
            except Exception:
                logger.exception(f"Failed to close music source {player.platform.name}")
        self.players.clear()

    async def start_login(
        self,
        platform: str,
        on_qr: Callable[[bytes], Awaitable[None]],
        on_message: Callable[[str], Awaitable[None]],
    ) -> None:
        """Start a QR login through callbacks shared by chat and web entry points.

        Args:
            platform: Login provider identifier, qqmusic or kugou.
            on_qr: Callback receiving PNG image bytes for display or storage.
            on_message: Callback receiving login results and errors, including
                messages delivered by polling after this method returns.

        Raises:
            ValueError: If the provider does not support QR login.
        """
        platform = platform.strip().lower()
        login = {"qqmusic": self.qqmusic_login, "kugou": self.kugou_login}.get(platform)
        if login is None:
            raise ValueError(f"QR login is unsupported for music source: {platform}")
        await login.start(on_qr, on_message)

    def get_player(self, name: str) -> BaseMusicPlayer | None:
        """Find an enabled source by its identifier or display name.

        Args:
            name: Case-insensitive source identifier or display name.

        Returns:
            The matching source, or None when it is unavailable.
        """
        name = name.strip().lower()
        return next(
            (
                player
                for player in self.players
                if name
                in {player.platform.name.lower(), player.platform.display_name.lower()}
            ),
            None,
        )

    async def resolve_song(self, player: BaseMusicPlayer, song: Song) -> Song:
        """Resolve replay metadata without switching song identity or source.

        Args:
            player: Enabled adapter that owns this song's playback entry.
            song: Stored metadata or a search result to resolve.

        Returns:
            The song with a fresh audio URL when available. Search-only adapters
            are refreshed by matching the exact source and platform song ID.
            A missing match leaves the song without an audio URL.

        Raises:
            Exception: If the source cannot resolve or refresh the song.
        """
        song = await player.fetch_extra(song)
        if song.audio_url or not song.name:
            return song
        source = (song.source or player.platform.name).strip().lower()
        candidates = await player.fetch_songs(
            keyword=" ".join(value for value in (song.name, song.artists) if value),
            limit=self.cfg.search_config.song_limit,
            extra=source,
        )
        match = next(
            (
                candidate
                for candidate in candidates
                if str(candidate.id).strip() == str(song.id).strip()
                and (candidate.source or player.platform.name).strip().lower() == source
            ),
            None,
        )
        if match is not None:
            for attribute in fields(Song):
                value = getattr(match, attribute.name)
                if value is not None:
                    setattr(song, attribute.name, value)
            song = await player.fetch_extra(song)
        return song

    def get_players_by_priority(
        self, platform: str = "", *, fallback: bool = True
    ) -> list[BaseMusicPlayer]:
        """Order sources for an optional preferred or exclusive source.

        Args:
            platform: Source identifier or display name; empty selects all sources.
            fallback: Whether other sources may follow the specified source.

        Returns:
            Enabled sources in search order.

        Raises:
            ValueError: If the specified source is unavailable.
        """
        players = sorted(self.players, key=lambda player: player.platform_cfg.priority)
        if not platform:
            return players
        selected_player = self.get_player(platform)
        if selected_player is None:
            raise ValueError(f"Music source is unavailable: {platform}")
        return [selected_player] + (
            [player for player in players if player is not selected_player]
            if fallback
            else []
        )

    @staticmethod
    def _pick_best_song(
        query: str, songs: list[Song], artist: str | None = None
    ) -> Song | None:
        """Select the best candidate only when its title and requested artist match.

        Args:
            query: User-provided song title, optionally followed by an artist.
            songs: Search results to rank.
            artist: Artist that must match when supplied separately.

        Returns:
            The best matching song, or None when no candidate confidently matches.
        """
        if not songs:
            return None

        def normalize(value: str) -> str:
            return re.sub(r"[\W_]+", "", value.casefold())

        normalized_query = normalize(query)
        expected_artist = normalize(artist or "")
        if not normalized_query:
            return songs[0] if not expected_artist else None

        matches: list[tuple[int, Song]] = []
        for song in songs:
            title = normalize(song.name or "")
            singer = normalize(song.artists or "")
            if not title:
                continue

            if title in normalized_query:
                extra_query = normalized_query.replace(title, "", 1)
            elif normalized_query in title:
                extra_query = ""
            else:
                continue

            if expected_artist and expected_artist not in singer:
                continue
            if extra_query and not expected_artist:
                if extra_query not in singer and extra_query not in title:
                    continue

            score = len(title) * 2
            if title == normalized_query:
                score += 100
            if expected_artist:
                score += 100
            matches.append((score, song))

        return max(matches, key=lambda match: match[0])[1] if matches else None

    async def search_songs(
        self,
        query: str,
        *,
        artist: str = "",
        platform: str = "",
        sub_source: str | None = None,
        limit: int | None = None,
        fallback: bool = True,
        require_match: bool = True,
    ) -> SearchResult:
        """Search sources until a candidate list satisfies the matching policy.

        Args:
            query: Song title or search keywords, optionally including an artist.
            artist: Artist that a reliable match must contain when provided.
            platform: Preferred or exclusive source identifier or display name.
            sub_source: Adapter-specific sub-source for the specified source only.
            limit: Candidate count per source; defaults to the search configuration.
            fallback: Whether to try other sources after the specified source.
            require_match: Whether results must contain a reliable match. Explicit
                source menus may display results without a reliable title match.

        Returns:
            Accepted candidates and their source, best match, and attempted sources.
            An unsuccessful result has no player or songs.

        Raises:
            ValueError: If the specified source is unavailable.
        """
        result = SearchResult()
        players = self.get_players_by_priority(platform, fallback=fallback)
        selected_player = self.get_player(platform) if platform else None
        for player in players:
            result.attempted_sources.append(player.platform.display_name)
            logger.debug(f"Searching {query} with {player.platform.display_name}")
            try:
                songs = await player.fetch_songs(
                    keyword=query,
                    limit=self.cfg.search_config.song_limit if limit is None else limit,
                    extra=sub_source if player is selected_player else None,
                )
            except Exception as exc:
                logger.warning(
                    f"Search via {player.platform.display_name} failed: {exc}"
                )
                continue
            if not songs:
                continue
            best_song = self._pick_best_song(query, songs, artist=artist)
            if require_match and best_song is None:
                continue
            result.player = player
            result.songs = songs
            result.best_song = best_song
            break
        return result

    async def fetch_comments(self, player: BaseMusicPlayer, song: Song) -> Song:
        """Populate an already selected song's popular comments from its source.

        Args:
            player: Source that provided the selected song.
            song: Song whose comments should be fetched. Existing comments are reused.

        Returns:
            The song with available comments. Source failures are logged and leave
            the song available for playback without comments.
        """
        if not song.comments:
            try:
                await player.fetch_comments(song)
            except Exception as exc:
                logger.warning(
                    f"Comments fetch via {player.platform.name} failed: {exc}"
                )
        return song

    async def find_comments(
        self,
        query: str,
        *,
        artist: str = "",
        platform: str = "",
        fallback: bool = True,
    ) -> SearchResult:
        """Find a reliable song with popular comments across enabled sources.

        Args:
            query: Song title or search keywords, optionally including an artist.
            artist: Artist that must match when provided.
            platform: Preferred or exclusive source identifier or display name.
            fallback: Whether to try other sources after the specified source.

        Returns:
            The matching song with comments and its source. An unsuccessful result
            contains only the sources attempted. Each source uses its own song ID.

        Raises:
            ValueError: If the specified source is unavailable.
        """
        result = SearchResult()
        for player in self.get_players_by_priority(platform, fallback=fallback):
            candidate = await self.search_songs(
                query, artist=artist, platform=player.platform.name, fallback=False
            )
            result.attempted_sources.extend(candidate.attempted_sources)
            song = candidate.best_song
            if song is None:
                continue
            song = await self.fetch_comments(player, song)
            if song.comments:
                result.player = player
                result.songs = candidate.songs
                result.best_song = song
                break
        return result

    async def find_lyrics(
        self,
        query: str,
        *,
        artist: str = "",
        platform: str = "",
        fallback: bool = True,
    ) -> SearchResult:
        """Find a reliable song with lyrics, continuing when lyrics are unavailable.

        Args:
            query: Song title or search keywords, optionally including an artist.
            artist: Artist that must match when provided.
            platform: Preferred or exclusive source identifier or display name.
            fallback: Whether to try other sources after the specified source.

        Returns:
            The matching song with resolved lyrics and its source. An unsuccessful
            result contains only the sources attempted.

        Raises:
            ValueError: If the specified source is unavailable.
        """
        result = SearchResult()
        for player in self.get_players_by_priority(platform, fallback=fallback):
            candidate = await self.search_songs(
                query, artist=artist, platform=player.platform.name, fallback=False
            )
            result.attempted_sources.extend(candidate.attempted_sources)
            song = candidate.best_song
            if song is None:
                continue
            try:
                await player.fetch_lyrics(song)
                if song.lyrics:
                    await player.resolve_lyrics(song)
            except Exception as exc:
                logger.warning(f"Lyrics fetch via {player.platform.name} failed: {exc}")
                continue
            if song.lyrics:
                result.player = player
                result.songs = candidate.songs
                result.best_song = song
                break
        return result
