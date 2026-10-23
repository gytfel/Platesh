"""Сборка и запуск: боты (polling), HTTP API, опрос CryptoBot."""

from __future__ import annotations

import asyncio
import html
import logging
import signal
from contextlib import suppress

from aiogram.exceptions import TelegramAPIError
from aiohttp import web

from .access import diagnose
from .bots import build_dispatcher
from .bots.registry import BotRegistry
from .bots.shop import bot_commands
from .config import Config, weak_keys
from .db import Database
from .net import mask_proxy
from .notifier import Notifier
from .payments import PaymentService
from .supervisor import BotProcess, start_bot_processes
from .web import create_app

log = logging.getLogger("platesh")


ACCESS_CHECK_INTERVAL = 60


async def crypto_poller(payments: PaymentService, interval: float) -> None:
    while True:
        try:
            await payments.poll_crypto()
        except Exception:
            log.exception("Ошибка при опросе CryptoBot")
        await asyncio.sleep(interval)


async def access_watcher(payments: PaymentService) -> None:
    """Напоминания о продлении и удаление из закрытых чатов по окончании подписки."""
    while True:
        try:
            await payments.access.process_expirations()
        except Exception:
            log.exception("Ошибка при проверке сроков доступа")
        await asyncio.sleep(ACCESS_CHECK_INTERVAL)


async def check_chats(registry: BotRegistry, payments: PaymentService, notifier: Notifier) -> None:
    for rt in registry.all():
        if not rt.cfg.chats():
            continue
        problems = await diagnose(rt)
        for problem in problems:
            log.warning("Бот «%s»: %s", rt.name, problem)
        if problems:
            await notifier.alert(
                rt, "⚠️ <b>Platesh: проверь закрытые каналы</b>\n\n" + "\n".join(f"• {html.escape(p)}" for p in problems)
            )


async def _wait_for_stop_signal() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()


async def run(cfg: Config) -> None:
    db = await Database.open(cfg.database)
    proxy = cfg.proxy_url
    if proxy:
        log.info("Связь с Telegram и CryptoBot — через прокси %s", mask_proxy(proxy))
    registry = await BotRegistry.connect(cfg.bots, proxy=proxy)
    if not registry.all():
        await db.close()
        raise SystemExit("Ни один бот не подключился — проверь токены в config.yaml")

    notifier = Notifier(cfg, db)
    payments = PaymentService(cfg, db, registry, notifier)
    dp = build_dispatcher(registry, payments)

    runner: web.AppRunner | None = None
    tasks: list[asyncio.Task[None]] = []
    processes: list[BotProcess] = []
    try:
        if cfg.server.enabled:
            runner = web.AppRunner(create_app(cfg, registry, payments), access_log=None)
            await runner.setup()
            await web.TCPSite(runner, cfg.server.host, cfg.server.port).start()
            log.info("HTTP API: http://%s:%s (вебхук CryptoBot: /webhooks/cryptobot)", cfg.server.host, cfg.server.port)
            if not cfg.server.api_key:
                log.warning("server.api_key не задан — главный ключ API отключён")
            for name in weak_keys(cfg):
                log.warning("%s слишком короткий — сгенерируй надёжный: python -m platesh genkey", name)

        if cfg.crypto_poll_interval > 0 and payments.crypto_clients():
            tasks.append(asyncio.create_task(crypto_poller(payments, cfg.crypto_poll_interval)))
        tasks.append(asyncio.create_task(access_watcher(payments)))
        await check_chats(registry, payments, notifier)
        # Код external-ботов (run: в config.yaml) — после HTTP API, чтобы им было к чему подключаться.
        processes = start_bot_processes(cfg, registry.all())

        managed = registry.managed()
        for rt in managed:
            try:
                await rt.bot.delete_webhook(drop_pending_updates=False)
                await rt.bot.set_my_commands(bot_commands(rt))
            except TelegramAPIError as exc:
                log.warning("Бот «%s»: не удалось подготовить бота: %s", rt.name, exc)

        for rt in managed:
            log.info("Бот @%s: витрина Platesh — напиши ему /start, чтобы увидеть товары и оплату", rt.username)
        running = {p.name for p in processes}
        for rt in registry.all():
            if not rt.cfg.managed and rt.name not in running:
                log.warning(
                    "Бот @%s (mode: external) сам не отвечает: оплата появится, когда запустится твой код бота "
                    "с platesh.router() и install_paywall(). Нет своего кода — поставь mode: managed "
                    "(или .\\run.bat setup → «У бота есть свой код?» — «н»)", rt.username,
                )
        log.info(
            "Platesh запущен: %s бот(ов), из них под управлением — %s, своим кодом — %s. Остановить: Ctrl+C",
            len(registry.all()), len(managed), len(processes),
        )
        if managed:
            await dp.start_polling(*(rt.bot for rt in managed), close_bot_session=False)
        else:
            await _wait_for_stop_signal()
    finally:
        log.info("Останавливаюсь...")
        await asyncio.gather(*(p.stop() for p in processes))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if runner is not None:
            await runner.cleanup()
        await notifier.close()
        await payments.close()
        await registry.close()
        await db.close()
