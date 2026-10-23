"""client/platesh_client.py против настоящего HTTP API Platesh."""

from __future__ import annotations

import datetime
import sys
import time
from itertools import count
from pathlib import Path

import pytest
from aiogram import Dispatcher
from aiogram.methods import AnswerPreCheckoutQuery, BanChatMember, SendInvoice, SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    PreCheckoutQuery,
    SuccessfulPayment,
    Update,
    User,
)

from platesh.models import PAID, Access
from platesh.web import create_app

from .conftest import BUYER_ID, CHANNEL_ID, build_env, make_bot, make_raw_config

sys.path.insert(0, str(Path(__file__).parent.parent / "client"))
from platesh_client import PlateshClient, PlateshError, verify_webhook  # noqa: E402


@pytest.fixture
async def platesh(env, aiohttp_server):
    server = await aiohttp_server(create_app(env.cfg, env.registry, env.payments))
    client = PlateshClient(str(server.make_url("/")), api_key="secret-key", bot="ext")
    yield client
    await client.close()


async def test_create_and_get(platesh, env):
    inv = await platesh.create_invoice(method="crypto", amount=3, title="Тест", user_id=BUYER_ID)
    assert inv["status"] == "pending" and inv["pay_url"]
    assert (await platesh.get_invoice(inv["id"], refresh=True))["id"] == inv["id"]
    with pytest.raises(PlateshError, match="не найден"):
        await platesh.create_invoice(method="stars", product_id="nope")


async def test_stars_router(platesh, env):
    inv = await platesh.create_invoice(method="stars", amount=10, title="Донат", user_id=BUYER_ID)
    external_bot = make_bot("333333:own-bot-code")  # «чужой» бот со своим кодом
    dp = Dispatcher()
    dp.include_router(platesh.router())

    query = PreCheckoutQuery(id="q", from_user=User(id=BUYER_ID, is_bot=False, first_name="A"),
                             currency="XTR", total_amount=10, invoice_payload=f"pl:{inv['id']}")
    await dp.feed_update(external_bot, Update(update_id=1, pre_checkout_query=query))
    assert external_bot.session.calls(AnswerPreCheckoutQuery)[-1].ok is True

    sp = SuccessfulPayment(currency="XTR", total_amount=10, invoice_payload=f"pl:{inv['id']}",
                           telegram_payment_charge_id="charge-1", provider_payment_charge_id="")
    msg = Message(message_id=1, date=datetime.datetime.now(), chat=Chat(id=BUYER_ID, type="private"),
                  from_user=User(id=BUYER_ID, is_bot=False, first_name="A", username="a"), successful_payment=sp)
    await dp.feed_update(external_bot, Update(update_id=2, message=msg))
    stored = await env.db.get_invoice(inv["id"])
    assert stored.status == PAID and stored.charge_id == "charge-1"

    # Счета, созданные не через Platesh, роутер не трогает.
    foreign = PreCheckoutQuery(id="q2", from_user=User(id=1, is_bot=False, first_name="B"),
                               currency="XTR", total_amount=10, invoice_payload="my-own-invoice")
    await dp.feed_update(external_bot, Update(update_id=3, pre_checkout_query=foreign))
    assert len(external_bot.session.calls(AnswerPreCheckoutQuery)) == 1


def test_verify_webhook():
    import hashlib
    import hmac

    body = b'{"event":"invoice_paid"}'
    sig = hmac.new(b"s", body, hashlib.sha256).hexdigest()
    assert verify_webhook(body, sig, "s")
    assert not verify_webhook(body, sig, "other")
    assert not verify_webhook(body, None, "s")


# ---------------------------------------------------------------------- пейволл, кнопки, охрана

_ids = count(5000)
OWN_TOKEN = "333333:own-bot-code"


@pytest.fixture
async def paid_env(tmp_path, aiohttp_server):
    raw = make_raw_config(tmp_path)
    raw["bots"][1]["api_key"] = "ext-key"
    raw["bots"][1]["products"] = [{
        "id": "premium", "title": "Премиум", "price": {"stars": 50, "crypto": 3}, "days": 30,
        "deliver": {"invite_chat_id": CHANNEL_ID},
    }]
    env = await build_env(tmp_path, raw)
    server = await aiohttp_server(create_app(env.cfg, env.registry, env.payments))
    client = PlateshClient(str(server.make_url("/")), api_key="ext-key", bot="ext")
    own_bot = make_bot(OWN_TOKEN)  # бот со своим кодом
    dp = Dispatcher()
    dp.include_router(client.router())
    client.install_paywall(dp, products=["premium"])
    handled = []

    @dp.message()
    async def business_logic(message: Message) -> None:
        handled.append(message.text)

    yield env, client, own_bot, dp, handled
    await client.close()
    await env.notifier.close(grace=1)
    await env.db.close()


def text_update(text, uid=BUYER_ID, chat_type="private"):
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=datetime.datetime.now(), chat=Chat(id=uid if chat_type == "private" else -5, type=chat_type),
        from_user=User(id=uid, is_bot=False, first_name="u"), text=text,
    ))


async def test_paywall_blocks_until_paid(paid_env):
    env, client, own_bot, dp, handled = paid_env
    await dp.feed_update(own_bot, text_update("привет"))
    assert handled == []
    offer = own_bot.session.calls(SendMessage)[-1]
    assert "после оплаты" in offer.text
    buttons = [r[0].callback_data for r in offer.reply_markup.inline_keyboard]
    assert buttons == ["platesh:pay:premium:stars", "platesh:pay:premium:crypto"]
    assert "/ 30 дн." in offer.reply_markup.inline_keyboard[0][0].text

    await dp.feed_update(own_bot, text_update("/paysupport"))  # Telegram требует отвечать на него всегда
    await dp.feed_update(own_bot, text_update("в группе", chat_type="group"))
    assert handled == ["/paysupport", "в группе"]

    # Нажал «Оплатить Stars» — Platesh отправил счёт от имени бота.
    cb = Update(update_id=next(_ids), callback_query=CallbackQuery(
        id="cb", from_user=User(id=BUYER_ID, is_bot=False, first_name="u"), chat_instance="c",
        data="platesh:pay:premium:stars",
        message=Message(message_id=1, date=datetime.datetime.now(), chat=Chat(id=BUYER_ID, type="private"), text="x"),
    ))
    await dp.feed_update(own_bot, cb)
    invoice = env.session("ext").calls(SendInvoice)[-1]
    assert invoice.chat_id == BUYER_ID and invoice.prices[0].amount == 50

    # Оплата прошла — бот начинает отвечать.
    await client.stars_paid(payload=invoice.payload, charge_id="c1", total_amount=50, currency="XTR", user_id=BUYER_ID)
    client.forget(BUYER_ID)
    await dp.feed_update(own_bot, text_update("теперь можно?"))
    assert handled[-1] == "теперь можно?"


async def test_paywall_closes_after_expiry(paid_env):
    env, client, own_bot, dp, handled = paid_env
    await env.db.save_access(Access("ext", BUYER_ID, "premium", expires_at=int(time.time()) - 1))
    await dp.feed_update(own_bot, text_update("а сейчас?"))
    assert handled == []


async def test_access_cache(paid_env, monkeypatch):
    env, client, own_bot, dp, handled = paid_env
    calls = []
    original = client._request

    async def counting(method, path, **kwargs):
        calls.append(path)
        return await original(method, path, **kwargs)

    monkeypatch.setattr(client, "_request", counting)
    await env.db.save_access(Access("ext", BUYER_ID, "premium", expires_at=None))
    assert await client.has_access(BUYER_ID, product_id="premium")
    assert await client.has_access(BUYER_ID, product_id="premium")
    assert calls.count("/api/v1/access") == 1  # второй раз — из кэша


async def test_client_guard_kicks_non_payers(paid_env):
    env, client, own_bot, dp, handled = paid_env
    user = User(id=999, is_bot=False, first_name="x")
    update = Update(update_id=next(_ids), chat_member=ChatMemberUpdated(
        chat=Chat(id=CHANNEL_ID, type="channel", title="VIP"), from_user=user, date=datetime.datetime.now(),
        old_chat_member=ChatMemberLeft(user=user), new_chat_member=ChatMemberMember(user=user),
    ))
    await dp.feed_update(own_bot, update)
    assert own_bot.session.calls(BanChatMember)[0].user_id == 999


async def test_example_paywall_bot(paid_env):
    """examples/paywall_bot.py: до оплаты — кнопки, после — обычные ответы, админ проходит всегда."""
    import importlib.util

    env, client, own_bot, _, _ = paid_env
    path = Path(__file__).parent.parent / "examples" / "paywall_bot.py"
    spec = importlib.util.spec_from_file_location("paywall_bot", path)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    dp = example.build_dispatcher(client, admins=[42])

    def answers():
        return [m.text for m in own_bot.session.calls(SendMessage)]

    await dp.feed_update(own_bot, text_update("/start"))
    assert "после оплаты" in answers()[-1]

    await dp.feed_update(own_bot, text_update("/start", uid=42))
    assert answers()[-1].startswith("Привет! Подписка активна")

    await env.db.save_access(Access("ext", BUYER_ID, "premium", expires_at=int(time.time()) + 3600))
    client.forget(BUYER_ID)
    await dp.feed_update(own_bot, text_update("эхо"))
    assert answers()[-1] == "Ты написал: эхо"


def test_from_env(monkeypatch):
    for name in ("PLATESH_URL", "PLATESH_KEY", "PLATESH_BOT", "PLATESH_ADMINS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(PlateshError, match="PLATESH_URL"):
        PlateshClient.from_env()
    monkeypatch.setenv("PLATESH_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("PLATESH_KEY", "k")
    monkeypatch.setenv("PLATESH_BOT", "bot1")
    monkeypatch.setenv("PLATESH_ADMINS", "1, 2")
    client = PlateshClient.from_env()
    assert client.base_url == "http://127.0.0.1:9000" and client.bot == "bot1" and client.admins == {1, 2}


async def test_admins_from_platesh_pass_paywall(paid_env):
    env, client, own_bot, dp, handled = paid_env
    client.admins = {BUYER_ID}  # как будто пришло из PLATESH_ADMINS
    dp2 = Dispatcher()
    dp2.include_router(client.router())
    client.install_paywall(dp2)
    seen = []

    @dp2.message()
    async def logic(message: Message) -> None:
        seen.append(message.text)

    await dp2.feed_update(own_bot, text_update("я админ"))
    assert seen == ["я админ"]
