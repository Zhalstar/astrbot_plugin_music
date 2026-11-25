import io
import re

from PIL import Image, ImageDraw, ImageFont

from .config import PluginConfig

_TIMESTAMP_RE = re.compile(r"\[\d{2}:\d{2}(?:\.\d{2,3})?\]")


class LyricsTheme:
    image_width: int = 1000
    font_size: int = 30
    line_spacing: int = 20
    horizontal_padding: int = 80
    vertical_padding: int = 50
    top_color: tuple[int, int, int] = (255, 250, 240)
    bottom_color: tuple[int, int, int] = (235, 255, 247)
    text_color: tuple[int, int, int] = (70, 70, 70)

    def load_font(self, font_path: str) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(font_path, self.font_size)


class LyricsRenderer:
    def __init__(
        self,
        config: PluginConfig,
        theme: LyricsTheme | None = None,
    ):
        self.theme = theme or LyricsTheme()
        self.font_path = config.font_path

    @staticmethod
    def _build_gradient_background(
        width: int,
        height: int,
        top_color: tuple[int, int, int],
        bottom_color: tuple[int, int, int],
    ) -> Image.Image:
        """Create a vertical linear gradient image without per-pixel Python loops."""
        pixels = bytearray(height * 3)
        for y in range(height):
            ratio = y / height
            idx = y * 3
            pixels[idx] = int(top_color[0] * (1 - ratio) + bottom_color[0] * ratio)
            pixels[idx + 1] = int(top_color[1] * (1 - ratio) + bottom_color[1] * ratio)
            pixels[idx + 2] = int(top_color[2] * (1 - ratio) + bottom_color[2] * ratio)
        strip = Image.frombytes("RGB", (1, height), bytes(pixels))
        return strip.resize((width, height), Image.Resampling.NEAREST)

    def draw_lyrics(
        self,
        lyrics: str,
        image_width: int | None = None,
        line_spacing: int | None = None,
        top_color: tuple[int, int, int] | None = None,
        bottom_color: tuple[int, int, int] | None = None,
        text_color: tuple[int, int, int] | None = None,
    ) -> bytes:
        theme = self.theme
        image_width = image_width if image_width is not None else theme.image_width
        line_spacing = line_spacing if line_spacing is not None else theme.line_spacing
        top_color = top_color or theme.top_color
        bottom_color = bottom_color or theme.bottom_color
        text_color = text_color or theme.text_color

        # Load the font only for the duration of a single render so it does not stay resident.
        font = theme.load_font(str(self.font_path))

        lines = lyrics.splitlines()
        cleaned_lines = [_TIMESTAMP_RE.sub("", line) or "" for line in lines]

        dummy_img = Image.new("RGB", (image_width, 1))
        draw = ImageDraw.Draw(dummy_img)
        line_layouts: list[tuple[str, int, int]] = []
        max_line_width = 0
        total_line_height = 0
        for line in cleaned_lines:
            text = line if line.strip() else "　"
            bbox = draw.textbbox((0, 0), text, font=font)
            line_width = bbox[2] - bbox[0]
            line_height = bbox[3]
            line_layouts.append((text, line_width, line_height))
            max_line_width = max(max_line_width, line_width)
            total_line_height += line_height

        image_width = int(
            max(image_width, max_line_width + theme.horizontal_padding * 2)
        )
        total_height = max(
            1,
            int(
                total_line_height
                + line_spacing * max(len(line_layouts) - 1, 0)
                + theme.vertical_padding * 2
            ),
        )

        img = self._build_gradient_background(
            image_width, total_height, top_color, bottom_color
        )
        draw = ImageDraw.Draw(img)

        y = theme.vertical_padding
        for text, text_width, line_height in line_layouts:
            draw.text(
                ((image_width - text_width) / 2, y),
                text,
                font=font,
                fill=text_color,
            )
            y += line_height + line_spacing

        img_bytes = io.BytesIO()
        img.save(img_bytes, format="JPEG")
        return img_bytes.getvalue()
