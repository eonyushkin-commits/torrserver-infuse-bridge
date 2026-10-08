"""Раскладка видеофайлов по папкам медиатеки на основе guessit.

Структура, которую лучше всего распознаёт Infuse:
    Movies/Title (Year)/Title (Year).strm
    Movies/Title (Year)/Title (Year) - Part 1.strm
    TV/Show/Season 01/Show S01E05.strm
    TV/Show/Show E05.strm            (эпизод без номера сезона)
    Other/<исходное имя>.strm        (guessit не смог разобрать имя)
"""

import logging
import os
import re

from guessit import guessit

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".m4v", ".mov", ".wmv", ".webm")

# Символы, недопустимые в именах файлов на популярных ФС и в путях WebDAV.
_FORBIDDEN_CHARS = re.compile(r'[\x00-\x1f/\\:*?"<>|]')


def sanitize(name: str) -> str:
    """Делает строку безопасной для использования как имя файла или папки."""
    name = _FORBIDDEN_CHARS.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "Unknown"


def _as_ints(value) -> list:
    """Приводит значение guessit (число или список чисел) к списку int."""
    values = value if isinstance(value, list) else [value]
    return [int(item) for item in values]


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def is_video(path: str) -> bool:
    return path.lower().endswith(VIDEO_EXTENSIONS)


def library_path(file_path: str):
    """Возвращает путь в медиатеке для видеофайла: кортеж папок и имя без расширения.
    Принимает путь внутри торрента: guessit учитывает и имена папок (например, год
    или сезон в названии каталога раздачи).
    Для сэмплов (коротких отрывков из раздачи) возвращает None — им не место в медиатеке.
    """
    stem = sanitize(os.path.splitext(os.path.basename(file_path))[0])

    try:
        info = guessit(file_path)
        if any(str(item).lower() == "sample" for item in _as_list(info.get("other"))):
            return None

        raw_title = str(info.get("title", "")).strip()
        if not raw_title:
            return ("Other", stem)
        title = sanitize(raw_title)

        episode = info.get("episode")
        season = info.get("season")
        if info.get("type") == "episode" or episode is not None:
            return _episode_path(title, season, episode, info.get("date"), stem)

        return _movie_path(title, info.get("year"), info.get("part", info.get("cd")))
    except Exception as exc:
        logger.warning("guessit не смог разобрать имя файла '%s': %s", file_path, exc)
        return ("Other", stem)


def _episode_path(show: str, season, episode, date, stem: str) -> tuple:
    season_dir = None
    if season is not None:
        season_num = _as_ints(season)[0]
        season_dir = f"Season {season_num:02d}"

    if episode is not None:
        episodes = _as_ints(episode)
        tag = f"E{episodes[0]:02d}"
        if len(episodes) > 1:
            tag += f"-E{episodes[-1]:02d}"
        if season_dir:
            return ("TV", show, season_dir, f"{show} S{season_num:02d}{tag}")
        return ("TV", show, f"{show} {tag}")

    if date is not None:
        return ("TV", show, f"{show} {date.isoformat()}")

    # Эпизод без номера (допматериалы, спецвыпуски): сохраняем исходное имя.
    if season_dir:
        return ("TV", show, season_dir, stem)
    return ("TV", show, stem)


def _movie_path(title: str, year, part) -> tuple:
    folder = f"{title} ({_as_ints(year)[0]})" if year is not None else title
    name = folder
    if part is not None:
        name = f"{folder} - Part {_as_ints(part)[0]}"
    return ("Movies", folder, name)
