"""Обновление без git: файлы программы заменяются, настройки и база — нет."""

from __future__ import annotations

import io
import zipfile

import pytest

from platesh.updater import UpdateError, update


def make_zip(path, files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Platesh-branch/", "")
        for name, content in files.items():
            archive.writestr(f"Platesh-branch/{name}", content)
    path.write_bytes(buffer.getvalue())
    return path.as_uri()


def test_update_replaces_code_but_keeps_settings(tmp_path):
    app = tmp_path / "Platesh"
    (app / "platesh").mkdir(parents=True)
    (app / "platesh" / "app.py").write_text("old")
    (app / "config.yaml").write_text("мои настройки")
    (app / ".env").write_text("TOKEN=secret")
    url = make_zip(tmp_path / "u.zip", {
        "platesh/app.py": "new",
        "platesh/new_module.py": "x",
        "run.bat": "@echo off\r\n",
        "config.yaml": "чужие настройки",
        ".env": "TOKEN=оттуда",
        "data/platesh.db": "чужая база",
        "../evil.txt": "мимо папки",
    })
    count = update(app, url, say=lambda _: None)
    assert count == 3
    assert (app / "platesh" / "app.py").read_text() == "new"
    assert (app / "run.bat").read_bytes() == b"@echo off\r\n"  # переводы строк Windows сохраняются
    assert (app / "config.yaml").read_text() == "мои настройки"
    assert (app / ".env").read_text() == "TOKEN=secret"
    assert not (app / "data").exists() and not (tmp_path / "evil.txt").exists()
    assert update(app, url, say=lambda _: None) == 0  # повторно — нечего обновлять


def test_update_reports_download_errors(tmp_path):
    with pytest.raises(UpdateError, match="не удалось скачать"):
        update(tmp_path, (tmp_path / "missing.zip").as_uri(), say=lambda _: None)
