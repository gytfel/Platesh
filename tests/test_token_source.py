"""Токен бота в одном месте: Platesh читает его из .env бота (token_from), своей копии нет."""

from __future__ import annotations

import os

import pytest

from platesh.config import ConfigError, load_config
from platesh.token_source import TokenSourceError, use_token_from

CONFIG = """# Создано мастером настройки
admins: [42]
server:
  api_key: ${PLATESH_API_KEY}
bots:
- name: arch_bot
  token: ${BOT1_TOKEN}
  mode: external
  api_key: ${BOT1_API_KEY}
  products:
  - id: premium
    title: Доступ
    price: {stars: 100}
"""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))  # load_config подхватывает .env — не пачкаем окружение


@pytest.fixture
def setup(tmp_path):
    platesh = tmp_path / "platesh"
    archiver = tmp_path / "archiver"
    platesh.mkdir()
    archiver.mkdir()
    (platesh / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (platesh / ".env").write_text(
        "PLATESH_API_KEY=master-key-master-key-master-key\nBOT1_TOKEN=111:AAA\nBOT1_API_KEY=bot-key-bot-key-bot-key-bot-key\n",
        encoding="utf-8")
    (archiver / ".env").write_text('BOT_TOKEN="111:AAA"\r\nDB_PATH=/x\n', encoding="utf-8")
    return platesh, archiver


def test_token_moves_to_bot_env(setup):
    platesh, archiver = setup
    out = use_token_from(platesh / "config.yaml", archiver / ".env")
    assert "только в" in out and "BOT1_TOKEN" in out

    env = (platesh / ".env").read_text(encoding="utf-8")
    assert "BOT1_TOKEN" not in env and "111:AAA" not in env  # копии токена у Platesh больше нет
    assert "BOT1_API_KEY=bot-key" in env and "PLATESH_API_KEY" in env
    text = (platesh / "config.yaml").read_text(encoding="utf-8")
    assert text.startswith("# Создано мастером настройки") and f"token_from: {archiver / '.env'}" in text
    assert (platesh / "config.yaml.bak").read_text(encoding="utf-8") == CONFIG

    os.environ.pop("BOT1_TOKEN", None)
    [bot] = load_config(platesh / "config.yaml").bots
    assert bot.token == "111:AAA" and bot.token_from == archiver / ".env"

    # Сменил токен в @BotFather — поменял только у бота, Platesh подхватит при запуске.
    (archiver / ".env").write_text("BOT_TOKEN=111:NEW\n", encoding="utf-8")
    assert load_config(platesh / "config.yaml").bots[0].token == "111:NEW"
    assert "уже берётся" in use_token_from(platesh / "config.yaml", archiver / ".env")


def test_mismatched_token_changes_nothing(setup):
    platesh, archiver = setup
    (archiver / ".env").write_text("BOT_TOKEN=222:OTHER\n", encoding="utf-8")
    with pytest.raises(TokenSourceError, match="токены отличаются"):
        use_token_from(platesh / "config.yaml", archiver / ".env")
    assert (platesh / "config.yaml").read_text(encoding="utf-8") == CONFIG
    assert "BOT1_TOKEN=111:AAA" in (platesh / ".env").read_text(encoding="utf-8")


def test_token_from_errors(setup):
    platesh, archiver = setup
    use_token_from(platesh / "config.yaml", archiver / ".env")
    (archiver / ".env").unlink()
    with pytest.raises(ConfigError, match="token_from: файл .* не найден"):
        load_config(platesh / "config.yaml")
    (archiver / ".env").write_text("DB_PATH=/x\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="нет строки BOT_TOKEN="):
        load_config(platesh / "config.yaml")


def test_wizard_keeps_token_from(setup):
    from platesh.setup_wizard import Wizard

    platesh, archiver = setup
    use_token_from(platesh / "config.yaml", archiver / ".env")
    answers = iter(["д", "", "", "", "д", "", "200", "", "", "н", "н"])  # перенастроить, везде Enter, цена 200
    Wizard(platesh / "config.yaml", ask=lambda prompt: next(answers), say=lambda text: None,
           check_bot=lambda token, proxy=None: ("ok", "arch_bot"),
           check_crypto=lambda token, proxy=None: ("ok", "x", False)).run()
    assert "111:AAA" not in (platesh / ".env").read_text(encoding="utf-8")
    [bot] = load_config(platesh / "config.yaml").bots
    assert bot.token_from == archiver / ".env" and bot.products["premium"].price_stars == 200
