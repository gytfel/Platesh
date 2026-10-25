from __future__ import annotations

import datetime
from itertools import count

import pytest
from aiogram.methods import (
    AnswerCallbackQuery,
    AnswerPreCheckoutQuery,
    CreateChatInviteLink,
    EditMessageText,
    SendInvoice,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, PreCheckoutQuery, SuccessfulPayment, Update, User

from platesh.bots import build_dispatcher
from platesh.models import PAID

from .conftest import ADMIN_ID, BUYER_ID

_ids = count(1)


def user(uid: int = BUYER_ID) -> User:
    return User(id=uid, is_bot=False, first_name="Покупатель", username=f"user{uid}")


def message(text: str | None = None, uid: int = BUYER_ID, **kwargs) -> Update:
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=datetime.datetime.now(), chat=Chat(id=uid, type="private"),
        from_user=user(uid), text=text, **kwargs,
    ))


def callback(data: str, uid: int = BUYER_ID) -> Update:
    msg = Message(message_id=next(_ids), date=datetime.datetime.now(), chat=Chat(id=uid, type="private"), text="...")
    return Update(update_id=next(_ids), callback_query=CallbackQuery(
        id=str(next(_ids)), from_user=user(uid), chat_instance="ci", data=data, message=msg,
    ))


@pytest.fixture
def dp(env):
    return build_dispatcher(env.registry, env.payments)


async def feed(env, dp, update, bot="shop"):
    await dp.feed_update(env.registry.get(bot).bot, update)


async def test_start_shows_catalog(env, dp):
    await feed(env, dp, message("/start"))
    sent = env.session().calls(SendMessage)[-1]
    assert sent.chat_id == BUYER_ID and "Привет" in sent.text and "/admin" not in sent.text
    buttons = [b for row in sent.reply_markup.inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ["p:vip", "p:stars_only"]
    assert "100 ⭐" in buttons[0].text and "4.99 USD" in buttons[0].text
    assert (await env.db.get_user("shop", BUYER_ID))["username"] == f"user{BUYER_ID}"


async def test_start_for_admin_has_hint(env, dp):
    await feed(env, dp, message("/start", uid=ADMIN_ID))
    assert "/admin" in env.session().calls(SendMessage)[-1].text


async def test_deep_link_opens_product(env, dp):
    await feed(env, dp, message("/start p_vip"))
    sent = env.session().calls(SendMessage)[-1]
    rows = sent.reply_markup.inline_keyboard
    assert [[b.callback_data for b in row] for row in rows] == [["pay:vip:stars"], ["coins:vip"], ["cat"]]

    # Цена в USD: по кнопке крипты — выбор монеты (по две в ряд) и «Назад» к товару.
    await feed(env, dp, callback("coins:vip"))
    coins = env.session().calls(EditMessageText)[-1]
    assert "4.99 USD" in coins.text and "Выбери монету" in coins.text
    assert [[b.callback_data for b in row] for row in coins.reply_markup.inline_keyboard] == [
        ["pay:vip:crypto:USDT", "pay:vip:crypto:TON"], ["p:vip"]]
    assert coins.reply_markup.inline_keyboard[0][1].text == "💎 TON"


async def test_product_card_and_back(env, dp):
    await feed(env, dp, callback("p:stars_only"))
    edited = env.session().calls(EditMessageText)[-1]
    assert "Гайд" in edited.text
    assert [b.callback_data for row in edited.reply_markup.inline_keyboard for b in row] == ["pay:stars_only:stars", "cat"]
    await feed(env, dp, callback("cat"))
    assert "Привет" in env.session().calls(EditMessageText)[-1].text


async def test_stars_purchase_through_bot(env, dp):
    await feed(env, dp, callback("pay:vip:stars"))
    invoice = env.session().calls(SendInvoice)[-1]
    assert invoice.chat_id == BUYER_ID and invoice.currency == "XTR" and invoice.prices[0].amount == 100

    query = PreCheckoutQuery(id="q1", from_user=user(), currency="XTR", total_amount=100, invoice_payload=invoice.payload)
    await feed(env, dp, Update(update_id=next(_ids), pre_checkout_query=query))
    answer = env.session().calls(AnswerPreCheckoutQuery)[-1]
    assert answer.ok is True

    sp = SuccessfulPayment(currency="XTR", total_amount=100, invoice_payload=invoice.payload,
                           telegram_payment_charge_id="tg1", provider_payment_charge_id="")
    await feed(env, dp, message(successful_payment=sp))
    inv = await env.db.get_invoice(invoice.payload[3:])
    assert inv.status == PAID and inv.charge_id == "tg1"
    assert env.session().calls(CreateChatInviteLink)

    # Тот же счёт повторно оплатить нельзя.
    query2 = PreCheckoutQuery(id="q2", from_user=user(), currency="XTR", total_amount=100, invoice_payload=invoice.payload)
    await feed(env, dp, Update(update_id=next(_ids), pre_checkout_query=query2))
    assert env.session().calls(AnswerPreCheckoutQuery)[-1].ok is False

    await feed(env, dp, message("/purchases"))
    assert "VIP — 100 ⭐" in env.session().calls(SendMessage)[-1].text


async def test_crypto_purchase_and_check_button(env, dp):
    await feed(env, dp, callback("pay:vip:crypto"))
    sent = env.session().calls(SendMessage)[-1]
    buttons = [b for row in sent.reply_markup.inline_keyboard for b in row]
    check_data = buttons[1].callback_data
    inv_id = check_data[4:]

    await feed(env, dp, callback(check_data))
    assert "не поступила" in env.session().calls(AnswerCallbackQuery)[-1].text

    env.crypto().set_status("900", "paid", paid_asset="USDT", paid_amount="4.99")
    await feed(env, dp, callback(check_data))
    assert "уже оплачен" in env.session().calls(AnswerCallbackQuery)[-1].text
    assert (await env.db.get_invoice(inv_id)).status == PAID

    # Чужой счёт проверить нельзя.
    await feed(env, dp, callback(check_data, uid=BUYER_ID + 1))
    assert "недействителен" in env.session().calls(AnswerCallbackQuery)[-1].text


async def test_crypto_coin_chosen_by_buyer(env, dp):
    """Цена 4.99 USD, покупатель выбрал TON — CryptoBot выставит счёт только в TON по курсу."""
    await feed(env, dp, callback("pay:vip:crypto:TON"))
    created = env.crypto().created[-1]
    assert created["is_fiat"] is True and created["currency"] == "USD" and created["accepted_assets"] == ("TON",)
    assert "Оплата в <b>TON</b>" in env.session().calls(SendMessage)[-1].text

    await feed(env, dp, callback("pay:vip:crypto:BTC"))  # монеты нет в accepted_assets
    answer = env.session().calls(AnswerCallbackQuery)[-1]
    assert answer.show_alert and "недоступен" in answer.text


async def test_unavailable_method(env, dp):
    await feed(env, dp, callback("pay:stars_only:crypto"))
    answer = env.session().calls(AnswerCallbackQuery)[-1]
    assert answer.show_alert and "недоступен" in answer.text


async def test_admin_commands(env, dp):
    inv = await env.payments.create_invoice(env.shop, method="stars", user_id=BUYER_ID, product=env.shop.cfg.products["vip"])
    await env.payments.process_stars_payment(env.shop, payload=inv.tg_payload, charge_id="c1", total_amount=100, currency="XTR", user_id=BUYER_ID)

    await feed(env, dp, message("/stats", uid=ADMIN_ID))
    stats = env.session().calls(SendMessage)[-1].text
    assert "100 ⭐ · 1 опл." in stats and "Все боты" in stats

    await feed(env, dp, message("/payments", uid=ADMIN_ID))
    assert inv.id in env.session().calls(SendMessage)[-1].text

    await feed(env, dp, message(f"/refund {inv.id}", uid=ADMIN_ID))
    assert "Возврат выполнен" in env.session().calls(SendMessage)[-1].text
    assert (await env.db.get_invoice(inv.id)).status == "refunded"


async def test_admin_commands_hidden_from_buyers(env, dp):
    await feed(env, dp, message("/stats"))
    # Обычный пользователь просто видит каталог.
    assert "Привет" in env.session().calls(SendMessage)[-1].text


async def test_paysupport(env, dp):
    await feed(env, dp, message("/paysupport"))
    assert env.session().calls(SendMessage)[-1].text == env.shop.cfg.text("support")


async def test_title_is_escaped_in_card(tmp_path):
    from .conftest import build_env, make_raw_config

    raw = make_raw_config(tmp_path)
    raw["bots"][0]["products"][1]["title"] = "Гайд <PDF> & чек-лист"
    env = await build_env(tmp_path, raw)
    try:
        dp = build_dispatcher(env.registry, env.payments)
        await feed(env, dp, callback("p:stars_only"))
        assert "Гайд &lt;PDF&gt; &amp; чек-лист" in env.session().calls(EditMessageText)[-1].text
    finally:
        await env.db.close()


async def test_not_modified_edit_is_ignored(env, dp):
    from aiogram.exceptions import TelegramBadRequest

    env.session().errors[EditMessageText] = TelegramBadRequest(EditMessageText(text="x"), "Bad Request: message is not modified")
    await feed(env, dp, callback("p:vip"))
    assert env.session().calls(AnswerCallbackQuery)  # обработчик дошёл до конца
    assert env.session().calls(SendMessage) == []


async def test_service_messages_do_not_trigger_catalog(env, dp):
    from aiogram.types import RefundedPayment

    refund = RefundedPayment(currency="XTR", total_amount=100, invoice_payload="pl:x", telegram_payment_charge_id="c")
    await feed(env, dp, message(refunded_payment=refund))
    assert env.session().calls(SendMessage) == []


def test_broken_custom_text_does_not_crash(env):
    env.shop.cfg.texts["pay_stars"] = "Оплатить {amount} {oops"
    assert env.shop.cfg.text("pay_stars", amount=5) == "Оплатить {amount} {oops"
