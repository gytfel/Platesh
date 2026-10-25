"""client/platesh_client.py против настоящего HTTP API Platesh."""

from __future__ import annotations

import datetime
import sys
import time
from itertools import count
from pathlib import Path

import pytest
from aiogram import Dispatcher
from aiogram.methods import AnswerCallbackQuery, AnswerPreCheckoutQuery, BanChatMember, EditMessageText, SendInvoice, SendMessage
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
from platesh_client import PlateshClient, PlateshError, _days, _plan_names, verify_webhook  # noqa: E402


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


def callback_update(data, uid=BUYER_ID):
    return Update(update_id=next(_ids), callback_query=CallbackQuery(
        id="cb", from_user=User(id=uid, is_bot=False, first_name="u"), chat_instance="c", data=data,
        message=Message(message_id=1, date=datetime.datetime.now(), chat=Chat(id=uid, type="private"), text="x"),
    ))


async def test_paywall_blocks_until_paid(paid_env):
    env, client, own_bot, dp, handled = paid_env
    await dp.feed_update(own_bot, text_update("привет"))
    assert handled == []
    offer = own_bot.session.calls(SendMessage)[-1]
    assert "Бот ограничен" in offer.text and "нужна подписка" in offer.text
    # Шаг 1: выбор подписки — только срок, цены — на следующем шаге.
    [[plan]] = offer.reply_markup.inline_keyboard
    assert plan.callback_data == "platesh:plan:premium" and plan.text == "📅 На 30 дней"

    # Шаг 2: то же сообщение меняется на выбор способа оплаты.
    await dp.feed_update(own_bot, callback_update("platesh:plan:premium"))
    step2 = own_bot.session.calls(EditMessageText)[-1]
    assert "Подписка на 30 дней" in step2.text and "способ оплаты" in step2.text and step2.parse_mode == "HTML"
    rows = step2.reply_markup.inline_keyboard
    assert [[b.callback_data for b in row] for row in rows] == [
        ["platesh:pay:premium:stars"], ["platesh:coins:premium"], ["platesh:plans"]]
    assert [r[0].text for r in rows[:2]] == ["⭐ Telegram Stars — 50 ⭐", "💎 CryptoBot — 3 $"]

    # Шаг 3: крипта в USD — выбор монеты (по две в ряд), «Назад» — к способам оплаты.
    await dp.feed_update(own_bot, callback_update("platesh:coins:premium"))
    coins = own_bot.session.calls(EditMessageText)[-1]
    assert "CryptoBot — <b>3 $</b>" in coins.text and "монету" in coins.text and coins.parse_mode == "HTML"
    assert [[b.callback_data for b in row] for row in coins.reply_markup.inline_keyboard] == [
        ["platesh:pay:premium:crypto:USDT", "platesh:pay:premium:crypto:TON"], ["platesh:plan:premium"]]
    assert [b.text for b in coins.reply_markup.inline_keyboard[0]] == ["💵 USDT", "💎 TON"]

    # «Назад» — снова список подписок.
    await dp.feed_update(own_bot, callback_update("platesh:plans"))
    back = own_bot.session.calls(EditMessageText)[-1]
    assert back.text == offer.text and back.reply_markup.inline_keyboard[0][0].callback_data == "platesh:plan:premium"
    assert handled == []

    await dp.feed_update(own_bot, text_update("/paysupport"))  # Telegram требует отвечать на него всегда
    await dp.feed_update(own_bot, text_update("в группе", chat_type="group"))
    assert handled == ["/paysupport", "в группе"]

    # Нажал «Telegram Stars» — Platesh отправил счёт от имени бота.
    await dp.feed_update(own_bot, callback_update("platesh:pay:premium:stars"))
    invoice = env.session("ext").calls(SendInvoice)[-1]
    assert invoice.chat_id == BUYER_ID and invoice.prices[0].amount == 50

    # Оплата прошла — бот начинает отвечать.
    await client.stars_paid(payload=invoice.payload, charge_id="c1", total_amount=50, currency="XTR", user_id=BUYER_ID)
    client.forget(BUYER_ID)
    await dp.feed_update(own_bot, text_update("теперь можно?"))
    assert handled[-1] == "теперь можно?"


async def test_pay_in_chosen_coin(paid_env):
    """Цена 3 USD, покупатель нажал «TON» — счёт CryptoBot только в TON, сумма по курсу."""
    env, client, own_bot, dp, handled = paid_env
    await dp.feed_update(own_bot, callback_update("platesh:pay:premium:crypto:TON"))
    created = env.payments.crypto(env.ext).created[-1]
    assert created["is_fiat"] is True and created["amount"] == 3 and created["accepted_assets"] == ("TON",)
    assert "Оплата в <b>TON</b>" in env.session("ext").calls(SendMessage)[-1].text

    await dp.feed_update(own_bot, callback_update("platesh:pay:premium:crypto:SOL"))  # монеты нет в настройках
    alert = own_bot.session.calls(AnswerCallbackQuery)[-1]
    assert alert.show_alert and "Оплата в Solana сейчас недоступна" in alert.text


async def test_renew_button_opens_payment_methods(paid_env):
    """«Продлить» из напоминания Platesh — сразу выбор способа оплаты новым сообщением."""
    env, client, own_bot, dp, handled = paid_env
    await dp.feed_update(own_bot, callback_update("platesh:buy:premium"))
    sent = own_bot.session.calls(SendMessage)[-1]
    assert sent.chat_id == BUYER_ID and "Подписка на 30 дней" in sent.text
    assert [b.callback_data for row in sent.reply_markup.inline_keyboard for b in row] == [
        "platesh:pay:premium:stars", "platesh:coins:premium", "platesh:plans"]
    assert own_bot.session.calls(EditMessageText) == []


def test_plan_names():
    assert [_days(n) for n in (1, 3, 5, 11, 21, 22, 30, 365)] == [
        "1 день", "3 дня", "5 дней", "11 дней", "21 день", "22 дня", "30 дней", "365 дней"]
    month, forever = {"id": "m", "title": "Месяц", "days": 30}, {"id": "f", "title": "Навсегда", "days": None}
    assert _plan_names(month, [month, forever]) == ("На 30 дней", "Подписка на 30 дней")
    assert _plan_names(forever, [month, forever]) == ("Навсегда", "Доступ навсегда")
    vip = {"id": "v", "title": "VIP", "days": 30}  # тот же срок — различаем по названию
    assert _plan_names(vip, [month, vip]) == ("VIP", "VIP — 30 дней")


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
    assert "нужна подписка" in answers()[-1]

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


# ---------------------------------------------------------------------- Telegram Business (боты-архиваторы)

OWNER = 7001
CUSTOMER = 7002


def business_dispatcher(client):
    from aiogram.types import BusinessConnection, BusinessMessagesDeleted

    dp = Dispatcher()
    dp.include_router(client.router())
    client.install_paywall(dp, products=["premium"])
    seen = []

    @dp.business_connection()
    async def on_connection(event: BusinessConnection) -> None:
        seen.append(("connection", event.user.id))

    @dp.business_message()
    async def on_business_message(message: Message) -> None:
        seen.append(("message", message.text))

    @dp.deleted_business_messages()
    async def on_deleted(event: BusinessMessagesDeleted) -> None:
        seen.append(("deleted", event.message_ids))

    return dp, seen


def business_message(text, connection="bc1"):
    # Сообщение от клиента владельцу аккаунта — бот видит его через бизнес-подключение.
    return Update(update_id=next(_ids), business_message=Message(
        message_id=next(_ids), date=datetime.datetime.now(), chat=Chat(id=CUSTOMER, type="private"),
        from_user=User(id=CUSTOMER, is_bot=False, first_name="клиент"), text=text, business_connection_id=connection,
    ))


def offers_to(bot, user_id):
    return [m.text for m in bot.session.calls(SendMessage) if m.chat_id == user_id]


async def test_business_archiving_needs_owner_payment(paid_env):
    from aiogram.types import BusinessMessagesDeleted

    env, client, own_bot, _, _ = paid_env
    own_bot.session.business["bc1"] = OWNER
    dp, seen = business_dispatcher(client)

    await dp.feed_update(own_bot, business_message("привет"))
    await dp.feed_update(own_bot, business_message("ещё одно"))
    deleted = Update(update_id=next(_ids), deleted_business_messages=BusinessMessagesDeleted(
        business_connection_id="bc1", chat=Chat(id=CUSTOMER, type="private"), message_ids=[1, 2]))
    await dp.feed_update(own_bot, deleted)
    assert seen == []  # без оплаты переписка боту не передаётся
    offers = offers_to(own_bot, OWNER)
    assert len(offers) == 1 and "сохранять переписку начнёт только после оформления подписки" in offers[0]  # без спама
    assert offers_to(own_bot, CUSTOMER) == []  # клиенту владельца ничего не пишем

    await env.db.save_access(Access("ext", OWNER, "premium", expires_at=int(time.time()) + 3600))
    client.forget(OWNER)
    await dp.feed_update(own_bot, business_message("после оплаты"))
    assert seen == [("message", "после оплаты")]


async def test_business_connection_passes_and_offers_payment(paid_env):
    from aiogram.types import BusinessConnection

    env, client, own_bot, _, _ = paid_env
    dp, seen = business_dispatcher(client)
    connection = BusinessConnection(id="bc2", user=User(id=OWNER, is_bot=False, first_name="o"),
                                    user_chat_id=OWNER, date=datetime.datetime.now(), is_enabled=True)
    await dp.feed_update(own_bot, Update(update_id=next(_ids), business_connection=connection))
    assert seen == [("connection", OWNER)]  # код бота должен узнать о подключении
    assert len(offers_to(own_bot, OWNER)) == 1
    order = [r for r in own_bot.session.requests if type(r).__name__ == "SendMessage" or r is None]
    assert "после оформления подписки" in order[-1].text  # предложение — после ответа самого бота
    # Владелец уже известен по событию подключения — лишний запрос в Telegram не нужен.
    await dp.feed_update(own_bot, business_message("x", connection="bc2"))
    assert seen == [("connection", OWNER)]


async def test_business_admin_is_exempt(paid_env):
    env, client, own_bot, _, _ = paid_env
    own_bot.session.business["bc3"] = OWNER
    client.admins = {OWNER}
    dp, seen = business_dispatcher(client)
    await dp.feed_update(own_bot, business_message("админ", connection="bc3"))
    assert seen == [("message", "админ")]


def test_from_env_reports_bad_settings(monkeypatch):
    monkeypatch.setenv("PLATESH_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("PLATESH_KEY", "")
    monkeypatch.setenv("PLATESH_BOT", "")
    with pytest.raises(PlateshError, match="PLATESH_KEY, PLATESH_BOT"):
        PlateshClient.from_env()
    monkeypatch.setenv("PLATESH_KEY", "k")
    monkeypatch.setenv("PLATESH_BOT", "b")
    monkeypatch.setenv("PLATESH_ADMINS", "ТВОЙ_ID")
    with pytest.raises(PlateshError, match="не Telegram ID"):
        PlateshClient.from_env()
