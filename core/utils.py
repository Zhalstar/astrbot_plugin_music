import re
from enum import IntEnum

from .model import Song


class SendMode(IntEnum):
    """Song send modes."""

    NATIVE_CARD = 1
    ARK_CARD = 2
    RECORD_LINK = 3
    RECORD_LOCAL = 4
    FILE_LINK = 5
    FILE_LOCAL = 6
    TEXT = 7


# Mode alias map.
MODE_MAP_CN: dict[str, SendMode] = {
    "原生卡片": SendMode.NATIVE_CARD,
    "卡片": SendMode.NATIVE_CARD,
    "native_card": SendMode.NATIVE_CARD,
    "card": SendMode.NATIVE_CARD,
    "签名卡片": SendMode.ARK_CARD,
    "ark_card": SendMode.ARK_CARD,
    "ark": SendMode.ARK_CARD,
    "语音链接": SendMode.RECORD_LINK,
    "record_link": SendMode.RECORD_LINK,
    "语音": SendMode.RECORD_LOCAL,
    "本地语音": SendMode.RECORD_LOCAL,
    "record": SendMode.RECORD_LOCAL,
    "record_local": SendMode.RECORD_LOCAL,
    "文件链接": SendMode.FILE_LINK,
    "file_link": SendMode.FILE_LINK,
    "文件": SendMode.FILE_LOCAL,
    "本地文件": SendMode.FILE_LOCAL,
    "file": SendMode.FILE_LOCAL,
    "file_local": SendMode.FILE_LOCAL,
    "文本": SendMode.TEXT,
    "text": SendMode.TEXT,
}


def pick_best_song(
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


def parse_user_input(arg: str) -> tuple[int, list[str] | None, str | None]:
    """Parse the selected song input format.

    Args:
        arg: Raw user input after the song name.

    Returns:
        A tuple containing the selected index, the chosen send modes, and an
        optional parsing error message.
    """
    parts = arg.split()
    index = 0
    way = None
    modes = None
    mode_map = {
        SendMode.NATIVE_CARD: ["native_card"],
        SendMode.ARK_CARD: ["ark_card"],
        SendMode.RECORD_LINK: ["record_link"],
        SendMode.RECORD_LOCAL: ["record_local"],
        SendMode.FILE_LINK: ["file_link"],
        SendMode.FILE_LOCAL: ["file_local"],
        SendMode.TEXT: ["text"],
    }

    # 情况1: 单个数字 "2"
    if len(parts) == 1 and parts[0].isdigit():
        index = int(parts[0])

    # 情况2: "数字 模式" 格式 "1 2"（数字 数字）
    elif len(parts) == 2 and parts[0].isdigit():
        index = int(parts[0])
        second_part = parts[1]

        # 尝试解析为数字
        if second_part.isdigit():
            mode_value = int(second_part)
            if 1 <= mode_value <= 7:
                way = SendMode(mode_value)
            else:
                return (
                    0,
                    None,
                    "模式数字应为 1-7：1原生卡片 2签名卡片 3语音链接 4本地语音 5文件链接 6本地文件 7文本",
                )
        else:
            # 尝试匹配文本模式
            way = MODE_MAP_CN.get(second_part)
            if way is None:
                return (
                    0,
                    None,
                    "未知模式「"
                    f"{second_part}」，可用模式：原生卡片/签名卡片/语音/语音链接/本地语音/文件/文件链接/本地文件/文本 "
                    "或 1/2/3/4/5/6/7",
                )
    modes = mode_map.get(way) if way else None
    return index, modes, None
