"""Обновление без git: python -m platesh update (или .\\run.bat update / ./run.sh update).

Скачивает свежий архив с GitHub и заменяет файлы программы. Настройки (config.yaml, .env),
база (data/) и установленные библиотеки (.venv/) не трогаются.
"""

from __future__ import annotations

import io
import os
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

UPDATE_URL = os.environ.get(
    "PLATESH_UPDATE_URL",
    "https://github.com/gytfel/Platesh/archive/refs/heads/claude/beautiful-johnson-i6jquz.zip",
)
PROTECTED = {"config.yaml", ".env", "data", ".venv", "venv"}


class UpdateError(Exception):
    pass


def update(target: Path, url: str = UPDATE_URL, say: Callable[[str], None] = print) -> int:
    """Возвращает число обновлённых файлов."""
    say(f"Скачиваю обновление: {url}")
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            data = resp.read()
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateError(f"не удалось скачать обновление: {exc}") from exc

    names = archive.namelist()
    if not names or "/" not in names[0]:
        raise UpdateError("неожиданный формат архива")
    root = names[0].split("/", 1)[0] + "/"
    target = target.resolve()
    updated = 0
    for info in archive.infolist():
        if info.is_dir() or not info.filename.startswith(root):
            continue
        relative = info.filename[len(root):]
        if not relative or relative.split("/", 1)[0] in PROTECTED:
            continue
        destination = (target / relative).resolve()
        if target not in destination.parents:
            continue  # путь вида ../ — мимо папки программы не пишем
        content = archive.read(info)
        if destination.is_file() and destination.read_bytes() == content:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        updated += 1
    return updated
