"""Сборка и запуск: боты (polling), HTTP API, опрос CryptoBot."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from aiogram.exceptions import TelegramAPIError
from aiohttp import web

from .bots import build_dispatcher
from .bots.registry import BotRegistry
from .bots.shop import bot_commands
from .config import Config
from .db import Database
from .notifier import Notifier
from .payments import PaymentService
from .web import create_app

log = logging.getLogger("platesh")


async def crypto_poller(payments: PaymentService, interval: float) -> None:
    while True:
        try:
            await payments.poll_crypto()
        except Exception:
            log.exception("Ошибка при опросе CryptoBot")
        await asyncio.sleep(interval)


async def _wait_for_stop_signal() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()


async def run(cfg: Config) -> None:
    db = await Database.open(cfg.database)
    registry = await BotRegistry.connect(cfg.bots)
    if not registry.all():
        await db.close()
        raise SystemExit("Ни один бот не подключился — проверь токены в config.yaml")

    notifier = Notifier(cfg, db)
    payments = PaymentService(cfg, db, registry, notifier)
    dp = build_dispatcher(registry, payments)

    runner: web.AppRunner | None = None
    tasks: list[asyncio.Task[None]] = []
    try:
        if cfg.server.enabled:
            runner = web.AppRunner(create_app(cfg, registry, payments), access_log=None)
            await runner.setup()
            await web.TCPSite(runner, cfg.server.host, cfg.server.port).start()
            log.info("HTTP API: http://%s:%s (вебхук CryptoBot: /webhooks/cryptobot)", cfg.server.host, cfg.server.port)
            if not cfg.server.api_key:
                log.warning("server.api_key не задан — /api/* отключён, работают только боты и вебхук CryptoBot")

        if cfg.crypto_poll_interval > 0 and payments.crypto_clients():
            tasks.append(asyncio.create_task(crypto_poller(payments, cfg.crypto_poll_interval)))

        managed = registry.managed()
        for rt in managed:
            try:
                await rt.bot.delete_webhook(drop_pending_updates=False)
                await rt.bot.set_my_commands(bot_commands(rt))
            except TelegramAPIError as exc:
                log.warning("Бот «%s»: не удалось подготовить бота: %s", rt.name, exc)

        log.info(
            "Platesh запущен: %s бот(ов), из них под управлением — %s. Остановить: Ctrl+C",
            len(registry.all()), len(managed),
        )
        if managed:
            await dp.start_polling(*(rt.bot for rt in managed), close_bot_session=False)
        else:
            await _wait_for_stop_signal()
    finally:
        log.info("Останавливаюсь...")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if runner is not None:
            await runner.cleanup()
        await notifier.close()
        await payments.close()
        await registry.close()
        await db.close()
