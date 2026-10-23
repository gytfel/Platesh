"""Служба systemd для постоянной работы на сервере."""

from __future__ import annotations

import pytest

from platesh import service
from platesh.service import ServiceError, install_service, render_unit


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    (tmp_path / "config.yaml").write_text("bots: []")
    return tmp_path


def test_unit_file(project, tmp_path, monkeypatch):
    monkeypatch.setattr(service.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "abram")
    calls = []
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    unit = install_service(project, project / "config.yaml", say=lambda _: None,
                           run=lambda cmd, check: calls.append(cmd), systemd_dir=systemd)
    text = unit.read_text(encoding="utf-8")
    assert f"WorkingDirectory={project}" in text and "User=abram" in text and "Restart=always" in text
    assert f"ExecStart={project}/.venv/bin/python -m platesh -c {project}/config.yaml" in text
    assert calls[0] == ["systemctl", "daemon-reload"] and ["systemctl", "enable", "--now", "platesh"] in calls


def test_needs_root_and_setup(project, monkeypatch):
    monkeypatch.setattr(service.os, "geteuid", lambda: 1000)
    with pytest.raises(ServiceError, match="sudo"):
        install_service(project, project / "config.yaml")
    monkeypatch.setattr(service.os, "geteuid", lambda: 0)
    with pytest.raises(ServiceError, match="setup"):
        install_service(project, project / "missing.yaml")


def test_render_defaults_to_root(project):
    assert "User=root" in render_unit(project, project / "config.yaml", "root")
