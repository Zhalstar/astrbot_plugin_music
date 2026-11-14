from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.api.web import error_response, json_response, request

from .config import PluginConfig
from .model import Song
from .music_service import MusicService

PLUGIN_NAME = "astrbot_plugin_music"


class WebController:
    """Register and handle music plugin view APIs."""

    def __init__(
        self, context: Context, cfg: PluginConfig, music_service: MusicService
    ):
        self.cfg = cfg
        self.music_service = music_service
        self.store = music_service.store
        routes = [
            ("/sources", self.list_sources, ["GET"], "List enabled music sources"),
            ("/search", self.search, ["POST"], "Search music for the plugin view"),
            ("/play", self.play, ["POST"], "Resolve audio for the plugin view"),
            ("/lyrics", self.lyrics, ["POST"], "Fetch lyrics for the plugin view"),
            ("/favorites", self.list_favorites, ["GET"], "List favorite songs"),
            ("/favorites", self.set_favorite, ["POST"], "Set song favorite state"),
            ("/history", self.list_history, ["GET"], "List playback history"),
            ("/history", self.record_play, ["POST"], "Confirm browser playback"),
            ("/history/clear", self.clear_history, ["POST"], "Clear playback history"),
            ("/history/random", self.random_history, ["GET"], "Pick a history song"),
            (
                "/history/<song_id>/delete",
                self.delete_history,
                ["POST"],
                "Delete song history",
            ),
            ("/playlists", self.list_playlists, ["GET"], "List playlists"),
            ("/playlists", self.create_playlist, ["POST"], "Create a playlist"),
            (
                "/playlists/import",
                self.import_playlist,
                ["POST"],
                "Import a remote playlist",
            ),
            ("/playlists/<playlist_id>", self.get_playlist, ["GET"], "Get a playlist"),
            (
                "/playlists/<playlist_id>/rename",
                self.rename_playlist,
                ["POST"],
                "Rename a playlist",
            ),
            (
                "/playlists/<playlist_id>/delete",
                self.delete_playlist,
                ["POST"],
                "Delete a playlist",
            ),
            (
                "/playlists/<playlist_id>/songs",
                self.list_playlist_songs,
                ["GET"],
                "List playlist songs",
            ),
            (
                "/playlists/<playlist_id>/songs",
                self.add_playlist_song,
                ["POST"],
                "Collect a song",
            ),
            (
                "/playlists/<playlist_id>/songs/<song_id>/remove",
                self.remove_playlist_song,
                ["POST"],
                "Remove a playlist song",
            ),
            (
                "/playlists/<playlist_id>/order",
                self.reorder_playlist_songs,
                ["POST"],
                "Reorder playlist songs",
            ),
        ]
        for route, handler, methods, description in routes:
            context.register_web_api(
                f"/{PLUGIN_NAME}{route}", handler, methods, description
            )

    @staticmethod
    def _positive_id(value: Any, name: str) -> int:
        """Validate IDs shared by resource paths and JSON references.

        Args:
            value: Integer or decimal string to validate.
            name: Field name included in validation errors.

        Returns:
            A positive SQLite integer ID.

        Raises:
            ValueError: If the value is not a valid positive ID.
        """
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError(f"{name} must be a positive integer")
        try:
            number = int(value)
        except ValueError:
            raise ValueError(f"{name} must be a positive integer") from None
        if not 0 < number <= 2**63 - 1:
            raise ValueError(f"{name} must be a positive SQLite integer")
        return number

    @staticmethod
    def _pagination() -> tuple[int, int]:
        """Validate common list query parameters.

        Returns:
            A limit between 1 and 200 and a nonnegative offset.

        Raises:
            ValueError: If either query parameter is invalid.
        """
        try:
            limit = int(request.query.get("limit", "50"))
            offset = int(request.query.get("offset", "0"))
        except ValueError:
            raise ValueError("limit and offset must be integers") from None
        if not 1 <= limit <= 200 or not 0 <= offset <= 2**63 - 1:
            raise ValueError(
                "limit must be between 1 and 200; offset must be nonnegative"
            )
        return limit, offset

    def _serialize_song(self, player: str, song: Song) -> dict[str, Any]:
        """Expose replay metadata consistently across search, history, and playlists.

        Args:
            player: Adapter identifier used to replay the song.
            song: Song whose public metadata should be returned.

        Returns:
            JSON-compatible song data without local paths, lyrics, or comments.
            available indicates whether the adapter is currently enabled.
        """
        return {
            **{
                name: getattr(song, name)
                for name in (
                    "name",
                    "artists",
                    "duration",
                    "cover_url",
                    "page_url",
                    "audio_url",
                    "card_type",
                    "note",
                    "album_id",
                    "album_audio_id",
                )
            },
            "id": str(song.id).strip(),
            "source": (song.source or player).strip().lower(),
            "player": player,
            "available": self.music_service.get_player(player) is not None,
        }

    async def _read_song(self, payload: Any) -> tuple[str, Song]:
        """Read a stored reference or validated song metadata from a request.

        Args:
            payload: Object containing stored_song_id or a song object. The play
                endpoint's existing song_id/platform form is also accepted.

        Returns:
            Adapter identifier and the selected song.

        Raises:
            ValueError: If the reference or metadata is invalid.
            LookupError: If a referenced stored song does not exist.
        """
        if not isinstance(payload, dict):
            raise ValueError("request body must be an object")
        if "stored_song_id" in payload:
            song_id = self._positive_id(payload["stored_song_id"], "stored_song_id")
            entry = await self.store.get_song(song_id)
            if entry is None:
                raise LookupError("stored song does not exist")
            return entry["player"], entry["song"]
        data = payload.get("song", payload)
        if not isinstance(data, dict):
            raise ValueError("song must be an object")
        song_id = data.get("id", data.get("song_id"))
        if isinstance(song_id, bool) or not isinstance(song_id, (str, int)):
            raise ValueError("song id must be a string or integer")
        song_id = str(song_id).strip()
        if not song_id or len(song_id) > 128:
            raise ValueError("song id must contain 1 to 128 characters")
        player = data.get("player", data.get("platform"))
        if not isinstance(player, str) or not player.strip() or len(player) > 64:
            raise ValueError("player must contain 1 to 64 characters")
        player = player.strip().lower()
        adapter = self.music_service.get_player(player)
        if adapter is not None:
            player = adapter.platform.name
        metadata = {}
        for name in (
            "source",
            "name",
            "artists",
            "cover_url",
            "page_url",
            "audio_url",
            "card_type",
            "note",
            "duration",
            "album_id",
            "album_audio_id",
        ):
            value = data.get(name)
            if value is None:
                continue
            if name in {"duration", "album_id", "album_audio_id"}:
                if type(value) is not int or not 0 <= value <= 2**63 - 1:
                    raise ValueError(f"{name} must be a nonnegative integer")
            elif not isinstance(value, str) or len(value) > 4096:
                raise ValueError(f"{name} must be a string of at most 4096 characters")
            metadata[name] = value
        metadata["source"] = (
            metadata.get("source") or player
        ).strip().lower() or player
        return player, Song(id=song_id, **metadata)

    async def _serialize_entries(
        self, entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Serialize a page with favorite state using one batch identity query.

        Args:
            entries: Store entries or search results containing player and Song.

        Returns:
            Public entries with is_favorite included in each nested song.
        """
        favorites = await self.store.favorite_keys()
        result = []
        for entry in entries:
            song = self._serialize_song(entry["player"], entry["song"])
            song["is_favorite"] = (
                song["player"],
                song["source"],
                song["id"],
            ) in favorites
            result.append(entry | {"song": song})
        return result

    async def list_favorites(self):
        """Return favorites independently from editable custom playlists."""
        try:
            limit, offset = self._pagination()
        except ValueError as exc:
            return error_response(str(exc))
        entries, total = await self.store.list_favorites(limit=limit, offset=offset)
        return json_response(
            {
                "items": await self._serialize_entries(entries),
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    async def set_favorite(self):
        """Set an explicit favorite state without recording a play."""
        payload = await request.json(default={})
        favorite = payload.get("favorite") if isinstance(payload, dict) else None
        if type(favorite) is not bool:
            return error_response("favorite must be a boolean")
        try:
            player, song = await self._read_song(payload)
            song_id = await self.store.set_favorite(player, song, favorite)
        except ValueError as exc:
            return error_response(str(exc))
        except LookupError as exc:
            return error_response(str(exc), status_code=404)
        return json_response({"song_id": song_id, "is_favorite": favorite})

    async def list_sources(self):
        """Return enabled sources in the same configured order as search fallback.

        Returns:
            Source identifiers and display names ordered by ascending priority.
        """
        return json_response(
            {
                "sources": [
                    {
                        "name": player.platform.name,
                        "display_name": player.platform.display_name,
                        "supports_playlist_import": player.supports_playlist_import,
                    }
                    for player in self.music_service.get_players_by_priority()
                ]
            }
        )

    async def search(self):
        """Search enabled music sources using view input."""
        payload = await request.json(default={})
        query = payload.get("query") if isinstance(payload, dict) else None
        platform = payload.get("platform", "") if isinstance(payload, dict) else ""
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            return error_response("query must contain 1 to 200 characters")
        if not isinstance(platform, str):
            return error_response("platform must be a string")
        try:
            result = await self.music_service.search_songs(
                query.strip(),
                platform=platform.strip(),
                limit=100,
                fallback=not platform.strip(),
                require_match=False,
            )
        except ValueError as exc:
            return error_response(str(exc))
        return json_response(
            {
                "source": result.player.platform.display_name
                if result.player
                else None,
                "attempted_sources": result.attempted_sources,
                "songs": [
                    entry["song"]
                    for entry in await self._serialize_entries(
                        [
                            {
                                "player": result.player.platform.name
                                if result.player
                                else platform.strip(),
                                "song": song,
                            }
                            for song in result.songs
                        ]
                    )
                ],
            }
        )

    async def lyrics(self):
        """Find lyrics for a song using the selected source policy."""
        payload = await request.json(default={})
        query = payload.get("query") if isinstance(payload, dict) else None
        artist = payload.get("artist", "") if isinstance(payload, dict) else ""
        platform = payload.get("platform", "") if isinstance(payload, dict) else ""
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            return error_response("query must contain 1 to 200 characters")
        if not isinstance(artist, str) or not isinstance(platform, str):
            return error_response("artist and platform must be strings")
        try:
            result = await self.music_service.find_lyrics(
                query.strip(),
                artist=artist.strip(),
                platform=platform.strip(),
                fallback=not platform.strip(),
            )
        except ValueError as exc:
            return error_response(str(exc))
        if result.best_song is None or not result.best_song.lyrics:
            return json_response(
                {"lyrics": None, "attempted_sources": result.attempted_sources}
            )
        return json_response(
            {
                "lyrics": result.best_song.lyrics,
                "source": result.player.platform.display_name
                if result.player
                else None,
            }
        )

    async def play(self):
        """Resolve a selected song's audio URL for the music view.

        Returns:
            The resolved audio URL, source display name, and replay metadata.
            Resolving audio does not record history; the browser confirms playback
            through POST /history after its audio element starts playing.
        """
        payload = await request.json(default={})
        try:
            platform, song = await self._read_song(payload)
        except ValueError as exc:
            return error_response(str(exc))
        except LookupError as exc:
            return error_response(str(exc), status_code=404)
        player = self.music_service.get_player(platform)
        if player is None:
            return error_response("music source is unavailable", status_code=404)

        try:
            song = await self.music_service.resolve_song(player, song)
        except Exception as exc:
            logger.warning(
                f"Plugin music view failed to resolve audio from {platform}: {exc}"
            )
            return error_response("failed to resolve audio", status_code=502)
        if not song.audio_url:
            return error_response("audio is unavailable for this song", status_code=502)
        return json_response(
            {
                "audio_url": song.audio_url,
                "source": player.platform.display_name,
                "song": (
                    await self._serialize_entries(
                        [{"player": player.platform.name, "song": song}]
                    )
                )[0]["song"],
            }
        )

    async def list_history(self):
        """Return a paginated history catalog filtered by player and actual source."""
        try:
            limit, offset = self._pagination()
        except ValueError as exc:
            return error_response(str(exc))
        filters = {
            "player": request.query.get("player"),
            "source": request.query.get("source"),
        }
        entries = await self.store.list_history(limit=limit, offset=offset, **filters) # type: ignore
        return json_response(
            {
                "items": await self._serialize_entries(entries),
                "total": await self.store.count_history(**filters),
                "limit": limit,
                "offset": offset,
            }
        )

    async def random_history(self):
        """Pick a history entry with the same optional source filters as the catalog."""
        entries = await self.store.list_history(
            limit=1,
            random_order=True,
            player=request.query.get("player"),
            source=request.query.get("source"),
        )
        entry = entries[0] if entries else None
        return json_response(
            {"item": (await self._serialize_entries([entry]))[0] if entry else None}
        )

    async def record_play(self):
        """Record one browser playback confirmation, never an audio-resolution request."""
        try:
            player, song = await self._read_song(await request.json(default={}))
            song_id = await self.store.record_play(player, song)
        except ValueError as exc:
            return error_response(str(exc))
        except LookupError as exc:
            return error_response(str(exc), status_code=404)
        return json_response({"song_id": song_id})

    async def delete_history(self, song_id: str):
        """Delete a history entry by its local song ID.

        Args:
            song_id: Local song ID from the resource path.

        Returns:
            A deletion acknowledgement, or a validation/not-found response.
        """
        try:
            entry = await self.store.get_song(self._positive_id(song_id, "song_id"))
        except ValueError as exc:
            return error_response(str(exc))
        if entry is None or not await self.store.delete_history(
            entry["player"], entry["song"].id, source=entry["song"].source
        ):
            return error_response("history entry does not exist", status_code=404)
        return json_response({"deleted": True})

    async def clear_history(self):
        """Clear all history while retaining songs and playlist memberships."""
        return json_response({"deleted": await self.store.clear_history()})

    async def list_playlists(self):
        """Return paginated playlists with song counts."""
        try:
            limit, offset = self._pagination()
        except ValueError as exc:
            return error_response(str(exc))
        return json_response(
            {
                "items": await self.store.list_playlists(limit=limit, offset=offset),
                "total": await self.store.count_playlists(),
                "limit": limit,
                "offset": offset,
            }
        )

    async def create_playlist(self):
        """Create an empty playlist using a nonempty name of at most 200 characters."""
        payload = await request.json(default={})
        name = payload.get("name") if isinstance(payload, dict) else None
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            return error_response("name must contain 1 to 200 characters")
        playlist_id = await self.store.create_playlist(name)
        return json_response(
            await self.store.get_playlist(playlist_id), status_code=201
        )

    async def import_playlist(self):
        """Fetch a remote playlist and save it atomically as a local playlist."""
        payload = await request.json(default={})
        reference = payload.get("reference") if isinstance(payload, dict) else None
        platform = payload.get("platform", "") if isinstance(payload, dict) else ""
        if (
            not isinstance(reference, str)
            or not reference.strip()
            or len(reference) > 2000
        ):
            return error_response("reference must contain 1 to 2000 characters")
        if not isinstance(platform, str) or len(platform) > 100:
            return error_response("platform must be a string of at most 100 characters")
        try:
            player, remote = await self.music_service.fetch_playlist(
                reference, platform=platform, limit=1000
            )
            name = remote.name.strip()[:200] or f"Imported playlist {remote.id}"
            playlist_id, imported, duplicates = await self.store.import_playlist(
                name, player.platform.name, remote.songs
            )
            playlist = await self.store.get_playlist(playlist_id)
        except (ValueError, NotImplementedError) as exc:
            return error_response(str(exc))
        except RuntimeError as exc:
            return error_response(str(exc), status_code=502)
        return json_response(
            {
                "playlist": playlist,
                "source": player.platform.name,
                "source_playlist_id": remote.id,
                "imported": imported,
                "duplicates": duplicates,
            },
            status_code=201,
        )

    async def get_playlist(self, playlist_id: str):
        """Return metadata and size for one playlist.

        Args:
            playlist_id: Local playlist ID from the resource path.

        Returns:
            Playlist metadata, or a validation/not-found response.
        """
        try:
            playlist = await self.store.get_playlist(
                self._positive_id(playlist_id, "playlist_id")
            )
        except ValueError as exc:
            return error_response(str(exc))
        if playlist is None:
            return error_response("playlist does not exist", status_code=404)
        return json_response(playlist)

    async def rename_playlist(self, playlist_id: str):
        """Rename a playlist without changing membership.

        Args:
            playlist_id: Local playlist ID from the resource path.

        Returns:
            An update acknowledgement, or a validation/not-found response.
        """
        payload = await request.json(default={})
        name = payload.get("name") if isinstance(payload, dict) else None
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            return error_response("name must contain 1 to 200 characters")
        try:
            updated = await self.store.rename_playlist(
                self._positive_id(playlist_id, "playlist_id"), name
            )
        except ValueError as exc:
            return error_response(str(exc))
        if not updated:
            return error_response("playlist does not exist", status_code=404)
        return json_response({"updated": True})

    async def delete_playlist(self, playlist_id: str):
        """Delete a playlist without deleting shared songs or history.

        Args:
            playlist_id: Local playlist ID from the resource path.

        Returns:
            A deletion acknowledgement, or a validation/not-found response.
        """
        try:
            deleted = await self.store.delete_playlist(
                self._positive_id(playlist_id, "playlist_id")
            )
        except ValueError as exc:
            return error_response(str(exc))
        if not deleted:
            return error_response("playlist does not exist", status_code=404)
        return json_response({"deleted": True})

    async def list_playlist_songs(self, playlist_id: str):
        """Return a playlist page in saved playback order.

        Args:
            playlist_id: Local playlist ID from the resource path.

        Returns:
            Entries with local song IDs, song metadata, positions, and added times.
        """
        try:
            playlist_id_int = self._positive_id(playlist_id, "playlist_id")
            limit, offset = self._pagination()
            entries = await self.store.list_playlist_songs(
                playlist_id_int, limit=limit, offset=offset
            )
        except ValueError as exc:
            return error_response(str(exc))
        except LookupError as exc:
            return error_response(str(exc), status_code=404)
        playlist = await self.store.get_playlist(playlist_id_int)
        if playlist is None:
            return error_response("playlist does not exist", status_code=404)
        return json_response(
            {
                "items": await self._serialize_entries(entries),
                "total": playlist["song_count"],
                "limit": limit,
                "offset": offset,
            }
        )

    async def add_playlist_song(self, playlist_id: str):
        """Collect a search result or stored song without recording playback.

        Args:
            playlist_id: Local playlist ID from the resource path.

        Returns:
            Whether a new membership was added; duplicates update metadata only.
        """
        try:
            playlist_id_int = self._positive_id(playlist_id, "playlist_id")
            player, song = await self._read_song(await request.json(default={}))
            added = await self.store.add_playlist_song(playlist_id_int, player, song)
        except ValueError as exc:
            return error_response(str(exc))
        except LookupError as exc:
            return error_response(str(exc), status_code=404)
        return json_response({"added": added})

    async def remove_playlist_song(self, playlist_id: str, song_id: str):
        """Remove one membership without affecting other references.

        Args:
            playlist_id: Local playlist ID from the resource path.
            song_id: Local song ID from the resource path.

        Returns:
            A deletion acknowledgement, or a validation/not-found response.
        """
        try:
            deleted = await self.store.remove_playlist_song(
                self._positive_id(playlist_id, "playlist_id"),
                self._positive_id(song_id, "song_id"),
            )
        except ValueError as exc:
            return error_response(str(exc))
        if not deleted:
            return error_response("playlist song does not exist", status_code=404)
        return json_response({"deleted": True})

    async def reorder_playlist_songs(self, playlist_id: str):
        """Replace the complete playlist order, rejecting stale or invalid membership.

        Args:
            playlist_id: Local playlist ID from the resource path.

        Returns:
            An update acknowledgement, or a validation/not-found response.
        """
        payload = await request.json(default={})
        song_ids = payload.get("song_ids") if isinstance(payload, dict) else None
        if not isinstance(song_ids, list):
            return error_response("song_ids must be an array of local song IDs")
        try:
            playlist_id_int = self._positive_id(playlist_id, "playlist_id")
            ids = [self._positive_id(value, "song_ids item") for value in song_ids]
            await self.store.reorder_playlist_songs(playlist_id_int, ids)
        except ValueError as exc:
            return error_response(str(exc))
        except LookupError as exc:
            return error_response(str(exc), status_code=404)
        return json_response({"updated": True})
