from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import User
from aiogram.utils.token import TokenValidationError

from ..config import BotConfig

log = logging.getLogger(__name__)

NETWORK_RETRY_DELAYS = (2.0, 5.0, 10.0)
NO_TELEGRAM_HINT = (
    "\nНет связи с Telegram ({error}).\n"
    "Похоже, api.telegram.org недоступен с этого компьютера (так бывает при блокировке у провайдера).\n"
    "Что сделать:\n"
    "  1) включи VPN в режиме «весь трафик» (TUN / VPN-режим) и запусти Platesh снова;\n"
    "  2) или укажи прокси в config.yaml, например:  proxy: socks5://127.0.0.1:10808\n"
    "     (адрес и порт смотри в настройках VPN-клиента: v2rayN, Hiddify, Clash...);\n"
    "  3) надёжнее всего — запустить Platesh на сервере (VPS) за границей."
)


@dataclass
class BotRuntime:
    cfg: BotConfig
    bot: Bot
    id: int
    username: str

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def link(self) -> str:
        return f"https://t.me/{self.username}"

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.cfg.admins


class BotRegistry:
    def __init__(self, runtimes: list[BotRuntime]) -> None:
        self._by_name = {rt.name: rt for rt in runtimes}
        self._by_id = {rt.id: rt for rt in runtimes}

    @classmethod
    async def connect(cls, configs: list[BotConfig], *, proxy: str | None = None,
                      retry_delays: tuple[float, ...] = NETWORK_RETRY_DELAYS) -> BotRegistry:
        """Создаёт Bot для каждого токена и проверяет его через getMe.

        Бот, токен которого Telegram отклонил, пропускается (с ошибкой в логе), остальные работают.
        Если нет связи с Telegram — несколько повторов, затем запуск прерывается.
        """
        runtimes = []
        for cfg in configs:
            try:
                bot = create_bot(cfg.token, proxy)
            except TokenValidationError:
                log.error("Бот «%s»: неверный формат токена — пропущен", cfg.name)
                continue
            try:
                me = await _get_me(bot, retry_delays)
            except TelegramNetworkError as exc:
                await bot.session.close()
                for rt in runtimes:
                    await rt.bot.session.close()
                raise SystemExit(NO_TELEGRAM_HINT.format(error=exc.message)) from None
            except TelegramAPIError as exc:
                log.error("Бот «%s»: Telegram отклонил токен (%s) — пропущен", cfg.name, exc.message)
                await bot.session.close()
                continue
            runtimes.append(BotRuntime(cfg=cfg, bot=bot, id=me.id, username=me.username or str(me.id)))
            log.info("Бот «%s» подключён: @%s (%s)", cfg.name, me.username, cfg.mode)
        return cls(runtimes)

    def get(self, name: str) -> BotRuntime | None:
        return self._by_name.get(name)

    def by_id(self, bot_id: int) -> BotRuntime | None:
        return self._by_id.get(bot_id)

    def all(self) -> list[BotRuntime]:
        return list(self._by_name.values())

    def managed(self) -> list[BotRuntime]:
        return [rt for rt in self._by_name.values() if rt.cfg.managed]

    async def close(self) -> None:
        for rt in self._by_name.values():
            await rt.bot.session.close()


def create_bot(token: str, proxy: str | None = None) -> Bot:
    session = AiohttpSession(proxy=proxy) if proxy else None
    return Bot(token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def _get_me(bot: Bot, retry_delays: tuple[float, ...]) -> User:
    for delay in retry_delays:
        try:
            return await bot.get_me()
        except TelegramNetworkError as exc:
            log.warning("Нет связи с Telegram (%s), повтор через %s с", exc.message, delay)
            await asyncio.sleep(delay)
    return await bot.get_me()
