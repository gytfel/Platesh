"""Запуск: ``python -m platesh`` (или ``python -m platesh check`` для проверки настроек)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import sys
from pathlib import Path

from aiogram.exceptions import TelegramAPIError
from aiogram.utils.token import TokenValidationError

from .access import diagnose
from .bots.registry import BotRuntime, create_bot
from .config import CRYPTO, MIN_KEY_LENGTH, STARS, Config, ConfigError, load_config, weak_keys
from .cryptopay import CryptoPayClient, CryptoPayError


async def check(cfg: Config) -> bool:
    """Проверяет токены ботов и CryptoBot, печатает сводку."""
    good = True
    crypto_checked: dict[tuple[str, bool], str] = {}
    print(f"База данных: {cfg.database}")
    print(f"HTTP API: {'выключен' if not cfg.server.enabled else f'{cfg.server.host}:{cfg.server.port}'}"
          f"{'' if cfg.server.api_key or not cfg.server.enabled else ' (api_key не задан — /api/* отключён)'}")
    print()
    for b in cfg.bots:
        try:
            bot = create_bot(b.token)
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
                client = CryptoPayClient(b.crypto.token, testnet=b.crypto.testnet)
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
    parser.add_argument("command", nargs="?", default="run", choices=["run", "check", "setup", "genkey"],
                        help="run — запустить, setup — настроить (вопросы в терминале), "
                             "check — проверить настройки, genkey — придумать ключ API")
    parser.add_argument("-c", "--config", default="config.yaml", help="путь к config.yaml")
    args = parser.parse_args(argv)

    if args.command == "genkey":
        print(secrets.token_urlsafe(32))
        return

    config_path = Path(args.config).expanduser().resolve()
    # Нет настроек и человек сидит у терминала — сразу запускаем мастер, а не ругаемся.
    first_run = args.command == "run" and not config_path.exists() and sys.stdin.isatty()
    if args.command == "setup" or first_run:
        from .setup_wizard import Wizard

        if not Wizard(config_path).run():
            return
        args.command = "run"

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
