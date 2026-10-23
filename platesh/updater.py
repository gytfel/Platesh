"""Обновление без git: python -m platesh update (или .\\run.bat update / ./run.sh update).

Скачивает свежий архив с GitHub и заменяет файлы программы. Настройки (config.yaml, .env),
база (data/) и установленные библиотеки (.venv/) не трогаются.
"""

from __future__ import annotations

import errno
import io
import os
import shutil
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

UPDATE_URL = os.environ.get(
    "PLATESH_UPDATE_URL",
    "https://github.com/gytfel/Platesh/archive/refs/heads/claude/beautiful-johnson-i6jquz.zip",
)
PROTECTED = {"config.yaml", ".env", "data", ".venv", "venv"}
SPACE_MARGIN = 20 * 1024 * 1024  # запас места, чтобы не забить диск под ноль
TMP_SUFFIX = ".platesh-new"


class UpdateError(Exception):
    pass


def no_space_message(path: Path) -> str:
    drive = Path(path.resolve().anchor or "/")
    return (f"на диске {drive} не хватает места. Освободи хотя бы 100 МБ "
            f"(очисти Корзину, папку Загрузки, временные файлы) и запусти обновление снова")


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

    plan: list[tuple[Path, zipfile.ZipInfo]] = []
    for info in archive.infolist():
        if info.is_dir() or not info.filename.startswith(root):
            continue
        relative = info.filename[len(root):]
        if not relative or relative.split("/", 1)[0] in PROTECTED:
            continue
        destination = (target / relative).resolve()
        if target not in destination.parents:
            continue  # путь вида ../ — мимо папки программы не пишем
        plan.append((destination, info))

    needed = sum(info.file_size for _, info in plan) + SPACE_MARGIN
    if shutil.disk_usage(target).free < needed:
        raise UpdateError(no_space_message(target))

    updated = 0
    for destination, info in plan:
        content = archive.read(info)
        if destination.is_file() and destination.read_bytes() == content:
            continue
        # Сначала пишем рядом и только потом подменяем: если запись оборвётся,
        # старый файл останется целым и программа не сломается.
        temporary = destination.with_name(destination.name + TMP_SUFFIX)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(content)
            os.replace(temporary, destination)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            if exc.errno == errno.ENOSPC:
                raise UpdateError(no_space_message(target) + f". Обновлено файлов до ошибки: {updated}") from exc
            raise UpdateError(f"не удалось записать {destination}: {exc}") from exc
        updated += 1
    return updated
