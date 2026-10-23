from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe

from platesh.bots import registry as registry_module
from platesh.bots.registry import BotRegistry
from platesh.config import parse_config

from .conftest import EXT_TOKEN, make_bot, make_raw_config


@pytest.fixture
def bots(monkeypatch):
    created = {}

    def fake_create_bot(token, proxy=None):
        created[token] = make_bot(token)
        return created[token]

    monkeypatch.setattr(registry_module, "create_bot", fake_create_bot)
    return created


@pytest.fixture
def configs(tmp_path):
    return parse_config(make_raw_config(tmp_path), tmp_path).bots


async def test_connect_all(bots, configs):
    reg = await BotRegistry.connect(configs, retry_delays=(0,))
    assert [rt.name for rt in reg.all()] == ["shop", "ext"]
    assert [rt.name for rt in reg.managed()] == ["shop"]
    assert reg.by_id(reg.get("ext").id).username == f"bot{reg.get('ext').id}"


async def test_rejected_token_is_skipped(bots, configs, monkeypatch):
    def create(token, proxy=None):
        bot = make_bot(token)
        if token == EXT_TOKEN:
            bot.session.errors[GetMe] = TelegramUnauthorizedError(GetMe(), "Unauthorized")
        return bot

    monkeypatch.setattr(registry_module, "create_bot", create)
    reg = await BotRegistry.connect(configs, retry_delays=(0,))
    assert [rt.name for rt in reg.all()] == ["shop"]


async def test_network_failure_aborts_start(configs, monkeypatch):
    def create(token, proxy=None):
        bot = make_bot(token)
        bot.session.errors[GetMe] = TelegramNetworkError(GetMe(), "DNS error")
        return bot

    monkeypatch.setattr(registry_module, "create_bot", create)
    with pytest.raises(SystemExit, match="Нет связи с Telegram"):
        await BotRegistry.connect(configs, retry_delays=(0, 0))
