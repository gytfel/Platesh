"""Постоянная работа на Linux-сервере: sudo ./run.sh service.

Ставит Platesh службой systemd: работает после закрытия терминала, сам запускается после
перезагрузки сервера и перезапускается при сбое.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

SERVICE_NAME = "platesh"

UNIT = """[Unit]
Description=Platesh: оплата Telegram Stars и CryptoBot для ботов
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={workdir}
ExecStart={python} -m platesh -c {config}
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""


class ServiceError(Exception):
    pass


def render_unit(project: Path, config: Path, user: str) -> str:
    return UNIT.format(user=user, workdir=project, python=project / ".venv" / "bin" / "python", config=config)


def install_service(
    project: Path,
    config: Path,
    *,
    say: Callable[[str], None] = print,
    run: Callable[..., object] = subprocess.run,
    systemd_dir: Path = Path("/etc/systemd/system"),
) -> Path:
    if os.name == "nt":
        raise ServiceError("служба нужна только на Linux-сервере. На Windows просто держи окно Platesh открытым")
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise ServiceError("нужны права администратора: запусти  sudo ./run.sh service")
    if not config.is_file():
        raise ServiceError("сначала настрой Platesh: ./run.sh setup")
    if not (project / ".venv" / "bin" / "python").exists():
        raise ServiceError("сначала запусти ./run.sh хотя бы раз — он установит зависимости")
    user = os.environ.get("SUDO_USER") or "root"
    unit_path = systemd_dir / f"{SERVICE_NAME}.service"
    unit_path.write_text(render_unit(project, config, user), encoding="utf-8")
    try:
        run(["systemctl", "daemon-reload"], check=True)
        run(["systemctl", "enable", "--now", SERVICE_NAME], check=True)
        run(["systemctl", "restart", SERVICE_NAME], check=True)  # подхватить новую версию, если служба уже была
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ServiceError(f"systemctl не сработал: {exc}") from exc
    say(
        "✅ Platesh работает как служба: продолжит работать после закрытия терминала\n"
        "   и сам запустится после перезагрузки сервера.\n\n"
        f"   Посмотреть журнал:   journalctl -u {SERVICE_NAME} -f\n"
        f"   Перезапустить:       systemctl restart {SERVICE_NAME}   (после setup или update)\n"
        f"   Остановить:          systemctl stop {SERVICE_NAME}\n"
        f"   Убрать из автозапуска: systemctl disable --now {SERVICE_NAME}\n\n"
        "   Пока служба работает, не запускай ./run.sh вручную — будут два Platesh сразу."
    )
    return unit_path
