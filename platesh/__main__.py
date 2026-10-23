"""Запуск: ``python -m platesh`` (или ``python -m platesh check`` для проверки настроек)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# Остальная программа импортируется внутри функций: команда update должна работать,
# даже если какой-то файл программы сломан или не хватает библиотеки.
if TYPE_CHECKING:
    from .config import Config


async def check(cfg: Config) -> bool:
    """Проверяет токены ботов и CryptoBot, печатает сводку."""
    from aiogram.exceptions import TelegramAPIError
    from aiogram.utils.token import TokenValidationError

    from .access import diagnose
    from .bots.registry import BotRuntime, create_bot
    from .config import CRYPTO, MIN_KEY_LENGTH, STARS, weak_keys
    from .cryptopay import CryptoPayClient, CryptoPayError
    from .net import mask_proxy

    good = True
    crypto_checked: dict[tuple[str, bool], str] = {}
    proxy = cfg.proxy_url
    print(f"База данных: {cfg.database}")
    print(f"Связь с Telegram: {'через прокси ' + mask_proxy(proxy) if proxy else 'напрямую'}")
    print(f"HTTP API: {'выключен' if not cfg.server.enabled else f'{cfg.server.host}:{cfg.server.port}'}"
          f"{'' if cfg.server.api_key or not cfg.server.enabled else ' (api_key не задан — /api/* отключён)'}")
    print()
    for b in cfg.bots:
        try:
            bot = create_bot(b.token, proxy)
        except TokenValidationError:
            print(f"❌ {b.name}: неверный формат токена")
            good = False
            continue
        problems: list[str] = []
        try:
            me = await bot.get_me()
            status = f"✅ {b.name}: @{me.username} ({b.mode})"
            problems = await diagnose(BotRuntime(cfg=b, bot=bot, id=me.id, username=me.username or ""))
        except TelegramAPIError as exc:
            status = f"❌ {b.name}: {exc}"
            good = False
        finally:
            await bot.session.close()
        methods = []
        if b.method_enabled(STARS):
            methods.append("Stars")
        if b.method_enabled(CRYPTO):
            key = (b.crypto.token, b.crypto.testnet)
            if key not in crypto_checked:
                client = CryptoPayClient(b.crypto.token, testnet=b.crypto.testnet, proxy=proxy)
                try:
                    app = await client.get_me()
                    crypto_checked[key] = f"CryptoBot «{app.get('name')}»{' testnet' if b.crypto.testnet else ''}"
                except CryptoPayError as exc:
                    crypto_checked[key] = f"CryptoBot ❌ {exc}"
                    good = False
                finally:
                    await client.close()
            methods.append(f"{crypto_checked[key]}, цены в {b.crypto.currency}")
        print(status)
        print(f"   оплата: {', '.join(methods) or 'нет включённых способов'}")
        print(f"   товаров: {len(b.products)}, админы: {', '.join(map(str, b.admins)) or 'не заданы'}")
        if b.notify_url:
            print(f"   notify_url: {b.notify_url}")
        if b.chats():
            print(f"   закрытые каналы: {len(b.chats())}, охрана входа: {'вкл' if b.guard else 'выкл'}")
        for problem in problems:
            print(f"   ⚠️ {problem}")
    for name in weak_keys(cfg):
        print(f"⚠️ {name} короче {MIN_KEY_LENGTH} символов — сгенерируй надёжный: python -m platesh genkey")
    return good


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="platesh", description="Платёжный шлюз Stars + CryptoBot для Telegram-ботов")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "check", "setup", "update", "service", "genkey"],
                        help="run — запустить, setup — настроить (вопросы в терминале), check — проверить настройки, "
                             "update — скачать новую версию, service — работать постоянно на Linux-сервере, "
                             "genkey — придумать ключ API")
    parser.add_argument("-c", "--config", default="config.yaml", help="путь к config.yaml")
    parser.add_argument("--no-start", action="store_true", help="setup: только настроить, не запускать")
    args = parser.parse_args(argv)

    if args.command == "genkey":
        print(secrets.token_urlsafe(32))
        return

    if args.command == "update":
        from .updater import UpdateError, update

        try:
            count = update(Path(__file__).resolve().parent.parent)
        except UpdateError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"✅ Готово, обновлено файлов: {count}. Настройки и база не тронуты. Запускай как обычно.")
        return

    config_path = Path(args.config).expanduser().resolve()

    if args.command == "service":
        from .service import ServiceError, install_service

        try:
            install_service(Path(__file__).resolve().parent.parent, config_path)
        except ServiceError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            sys.exit(1)
        return
    # Нет настроек и человек сидит у терминала — сразу запускаем мастер, а не ругаемся.
    first_run = args.command == "run" and not config_path.exists() and sys.stdin.isatty()
    if args.command == "setup" or first_run:
        from .setup_wizard import Wizard

        if not Wizard(config_path).run(offer_start=not args.no_start):
            return
        args.command = "run"

    from .config import ConfigError, load_config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Ошибка в настройках: {exc}", file=sys.stderr)
        sys.exit(2)
    logging.getLogger().setLevel(getattr(logging, cfg.log_level, logging.INFO))
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    if args.command == "check":
        sys.exit(0 if asyncio.run(check(cfg)) else 1)

    from .app import run

    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
