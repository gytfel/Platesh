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


def test_not_enough_space_is_detected_before_writing(tmp_path, monkeypatch):
    import shutil as shutil_module

    app = tmp_path / "app"
    app.mkdir()
    (app / "main.py").write_text("old")
    url = make_zip(tmp_path / "u.zip", {"main.py": "new"})
    monkeypatch.setattr("platesh.updater.shutil.disk_usage",
                        lambda path: shutil_module._ntuple_diskusage(100, 100, 0))
    with pytest.raises(UpdateError, match="не хватает места"):
        update(app, url, say=lambda _: None)
    assert (app / "main.py").read_text() == "old"


def test_disk_full_during_write_keeps_old_files_intact(tmp_path, monkeypatch):
    import errno
    from pathlib import Path

    app = tmp_path / "app"
    app.mkdir()
    (app / "a.py").write_text("old a")
    (app / "b.py").write_text("old b")
    url = make_zip(tmp_path / "u.zip", {"a.py": "new a", "b.py": "new b"})
    original = Path.write_bytes

    def write_bytes(self, data):
        if self.name == "b.py.platesh-new":
            raise OSError(errno.ENOSPC, "No space left on device")
        return original(self, data)

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    with pytest.raises(UpdateError, match="не хватает места.*до ошибки: 1"):
        update(app, url, say=lambda _: None)
    assert (app / "a.py").read_text() == "new a"
    assert (app / "b.py").read_text() == "old b"  # не обнулился
    assert not list(app.glob("*.platesh-new"))


def test_low_disk_warning(tmp_path, monkeypatch, caplog):
    import shutil as shutil_module

    from platesh import app

    monkeypatch.setattr(app.shutil, "disk_usage", lambda path: shutil_module._ntuple_diskusage(10**9, 10**9, 5 * 1024 * 1024))
    app.warn_low_disk(tmp_path / "data")
    assert any("осталось 5 МБ" in r.getMessage() for r in caplog.records)
