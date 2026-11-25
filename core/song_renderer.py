import asyncio
import html
import re
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from astrbot.api import logger

from .config import PluginConfig
from .model import Song

_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")


class CardTheme:
    card_width: int = 220
    card_height: int = 278
    thumb_height: int = 220
    margin: int = 16
    corner_radius: int = 10
    font_size: int = 16
    card_bg: str = "#ffffff"
    canvas_bg: str = "#f5f5f5"
    title_color: str = "#000000"
    sub_text_color: str = "#666666"
    overlay_text_color: str = "#ffffff"
    gradient_height: int = 40
    gradient_max_alpha: int = 180

    def load_font(self, font_path: str) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(font_path, self.font_size)


class CardRenderer:
    def __init__(
        self,
        config: PluginConfig,
        theme: CardTheme | None = None,
    ):
        self.cfg = config
        self.theme = theme or CardTheme()

    def format_count(self, count: int) -> str:
        if count >= 10000:
            return f"{count / 10000:.1f}w"
        if count >= 1000:
            return f"{count / 1000:.1f}k"
        return str(count)

    @staticmethod
    def _clean_title(raw_title: str) -> str:
        """Strip HTML tags and unescape entities without invoking a DOM parser."""
        return html.unescape(_HTML_TAG_RE.sub("", raw_title))

    @staticmethod
    def _build_overlay(theme: CardTheme) -> Image.Image:
        """Build the bottom alpha-gradient overlay shared across cards in one render."""
        alpha_strip = bytes(
            int(theme.gradient_max_alpha * (y / theme.gradient_height))
            for y in range(theme.gradient_height)
        )
        alpha_gradient = Image.frombytes(
            "L", (1, theme.gradient_height), alpha_strip
        ).resize((theme.card_width, theme.gradient_height), Image.Resampling.NEAREST)
        overlay = Image.new(
            "RGBA",
            (theme.card_width, theme.gradient_height),
            color=(0, 0, 0, 255),
        )
        overlay.putalpha(alpha_gradient)
        return overlay

    @staticmethod
    def _build_card_mask(theme: CardTheme) -> Image.Image:
        """Build the rounded-rectangle alpha mask shared across cards in one render."""
        mask = Image.new("L", (theme.card_width, theme.card_height), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle(
            (0, 0, theme.card_width, theme.card_height),
            radius=theme.corner_radius,
            fill=255,
        )
        return mask

    def _draw_card_sync(
        self,
        media: dict,
        index: int,
        cover_map: dict[str, Image.Image],
        font: ImageFont.FreeTypeFont,
        overlay: Image.Image,
        mask: Image.Image,
    ) -> Image.Image:
        theme = self.theme
        try:
            card = Image.new(
                "RGBA",
                (theme.card_width, theme.card_height),
                theme.card_bg,
            )
            draw = ImageDraw.Draw(card)

            pic_url = str(media.get("cover") or media.get("pic") or "")
            thumb = cover_map.get(pic_url)
            if thumb is None:
                thumb = Image.new(
                    "RGB",
                    (theme.card_width, theme.thumb_height),
                    "#e5e5e5",
                )
            elif thumb.size != (theme.card_width, theme.thumb_height):
                thumb = thumb.resize((theme.card_width, theme.thumb_height))
            card.paste(thumb, (0, 0))

            card.paste(
                overlay,
                (0, theme.thumb_height - theme.gradient_height),
                overlay,
            )

            draw.text(
                (8, theme.thumb_height - 20),
                self.format_count(int(media.get("play", 0) or 0)),
                font=font,
                fill=theme.overlay_text_color,
            )
            draw.text(
                (theme.card_width - 40, theme.thumb_height - 20),
                str(media.get("duration") or "0:00"),
                font=font,
                fill=theme.overlay_text_color,
            )

            raw_title = self._clean_title(str(media.get("title") or ""))
            title = (
                raw_title[:18] + "\n" + raw_title[18:36] + "..."
                if len(raw_title) > 36
                else raw_title[:18] + "\n" + raw_title[18:]
            )
            draw.text(
                (8, theme.thumb_height + 8),
                title,
                font=font,
                fill=theme.title_color,
            )

            draw.text(
                (8, theme.card_height - 30),
                self._build_author_text(media),
                font=font,
                fill=theme.sub_text_color,
            )

            draw.text(
                (theme.card_width - 20, theme.card_height - 25),
                str(index),
                font=font,
                fill=theme.sub_text_color,
            )

            card.putalpha(mask)
            return card

        except Exception as exc:
            logger.error(f"render card failed: {exc}")
            return Image.new(
                "RGBA",
                (theme.card_width, theme.card_height),
                theme.card_bg,
            )

    async def draw_card(
        self,
        media: dict,
        index: int,
        cover_map: dict[str, Image.Image],
    ) -> Image.Image:
        def _render_single() -> Image.Image:
            theme = self.theme
            font = theme.load_font(str(self.cfg.font_path))
            return self._draw_card_sync(
                media,
                index,
                cover_map,
                font,
                self._build_overlay(theme),
                self._build_card_mask(theme),
            )

        return await asyncio.to_thread(_render_single)

    def _render_list_sync(
        self,
        media_list: list,
        cover_map: dict[str, Image.Image],
        jpeg_quality: int,
    ) -> bytes:
        theme = self.theme
        # Load the font and static masks once for this render so they do not stay resident.
        font = theme.load_font(str(self.cfg.font_path))
        overlay = self._build_overlay(theme)
        mask = self._build_card_mask(theme)

        per_row = max(1, self.cfg.search_config.cards_per_row)
        num_rows = max(1, (len(media_list) + per_row - 1) // per_row)
        total_width = per_row * theme.card_width + (per_row + 1) * theme.margin
        row_height = theme.card_height + 2 * theme.margin
        total_height = num_rows * row_height

        canvas = Image.new("RGB", (total_width, total_height), theme.canvas_bg)
        for i, media in enumerate(media_list):
            card = self._draw_card_sync(
                media,
                index=i + 1,
                cover_map=cover_map,
                font=font,
                overlay=overlay,
                mask=mask,
            )
            row_idx, col_idx = divmod(i, per_row)
            x = theme.margin + col_idx * (theme.card_width + theme.margin)
            y = row_idx * row_height + theme.margin
            canvas.paste(card, (x, y), card)

        buffer = BytesIO()
        canvas.save(buffer, format="JPEG", quality=jpeg_quality)
        return buffer.getvalue()

    async def render_list_image(
        self,
        media_list: list,
        cover_map: dict[str, Image.Image],
        jpeg_quality: int = 80,
    ) -> bytes:
        return await asyncio.to_thread(
            self._render_list_sync,
            media_list,
            cover_map,
            jpeg_quality,
        )

    async def render_song_list_image(
        self,
        songs: list[Song],
        cover_map: dict[str, Image.Image],
        jpeg_quality: int = 80,
    ) -> bytes:
        media_list = [
            {
                "cover": song.cover_url,
                "title": song.name,
                "author": song.artists,
                "duration": self._format_duration(song.duration),
                "play": 0,
            }
            for song in songs
        ]
        return await self.render_list_image(
            media_list, cover_map, jpeg_quality=jpeg_quality
        )

    @staticmethod
    def _build_author_text(media: dict) -> str:
        return str(media.get("author") or "").strip() or "-"

    @staticmethod
    def _format_duration(duration_ms: int | None) -> str:
        if not duration_ms:
            return "0:00"
        duration = duration_ms // 1000
        minutes = duration // 60
        seconds = duration % 60
        return f"{minutes}:{seconds:02d}"
