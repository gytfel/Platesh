"""Пример: бот со своим кодом (aiogram 3) + пейволл Platesh.

Пока пользователь не оплатил товар premium, бот не отвечает, а показывает кнопки оплаты
(⭐ Stars / 💎 CryptoBot). После оплаты бот работает как обычно, а когда подписка
закончится — снова закроется.

Запуск (Platesh уже запущен в другом терминале, в config.yaml есть бот my_own_bot):

    Linux/macOS:  BOT_TOKEN=... PLATESH_KEY=... ADMINS=123456789 python examples/paywall_bot.py
    Windows:      set BOT_TOKEN=...  &  set PLATESH_KEY=...  &  python examples\\paywall_bot.py

В своём боте нужны только строки, помеченные ★.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))
from platesh_client import PlateshClient  # noqa: E402  ★ файл client/platesh_client.py

# ----------------------------------------------------------------- твой код (как был)

router = Router()


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer("Привет! Подписка активна — бот в твоём распоряжении.")


@router.message(F.text)
async def echo(message: Message) -> None:
    await message.answer(f"Ты написал: {message.text}")


# ----------------------------------------------------------------- подключение Platesh


def build_dispatcher(platesh: PlateshClient, admins: list[int]) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(platesh.router())  # ★ оплаты Stars, кнопки «Оплатить»/«Продлить»
    platesh.install_paywall(dp, products=["premium"], exempt_users=admins)  # ★ пейволл
    dp.include_router(router)  # твои обработчики — без изменений
    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = Bot(os.environ["BOT_TOKEN"])
    platesh = PlateshClient(  # ★
        os.environ.get("PLATESH_URL", "http://127.0.0.1:8080"),
        api_key=os.environ["PLATESH_KEY"],  # ключ этого бота (bots[].api_key в config.yaml)
        bot=os.environ.get("PLATESH_BOT", "my_own_bot"),  # name бота в config.yaml
    )
    admins = [int(x) for x in os.environ.get("ADMINS", "").replace(",", " ").split()]
    try:
        await build_dispatcher(platesh, admins).start_polling(bot)
    finally:
        await platesh.close()


if __name__ == "__main__":
    asyncio.run(main())
