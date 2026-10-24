"""Пример: бот со своим кодом (aiogram 3) + пейволл Platesh.

Пока пользователь не оплатил товар premium, бот не отвечает, а предлагает подписку: сначала
кнопки подписок, затем выбор способа оплаты (⭐ Stars / 💎 CryptoBot). После оплаты бот работает как обычно, а когда подписка
закончится — снова закроется.

Удобнее всего, чтобы бота запускал сам Platesh (мастер `./run.sh setup` спросит путь к файлу бота):
тогда адрес, ключ и админы придут в переменных окружения и PlateshClient.from_env() их подхватит.

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
    bot = Bot(os.environ["BOT_TOKEN"])  # токен твоего бота — как у тебя и было
    platesh = PlateshClient.from_env()  # ★ адрес, ключ и админы — от Platesh
    try:
        await build_dispatcher(platesh, admins=[]).start_polling(bot)
    finally:
        await platesh.close()


if __name__ == "__main__":
    asyncio.run(main())
