"""Подписки, напоминания, удаление из канала, охрана входа, ручная выдача доступа."""

from __future__ import annotations

import datetime
import time
from itertools import count

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    ApproveChatJoinRequest,
    BanChatMember,
    CreateChatInviteLink,
    DeclineChatJoinRequest,
    SendMessage,
    UnbanChatMember,
)
from aiogram.types import Chat, ChatJoinRequest, ChatMemberLeft, ChatMemberMember, ChatMemberUpdated, Message, Update, User

from platesh.access import DAY, diagnose
from platesh.bots import build_dispatcher
from platesh.models import Access

from .conftest import ADMIN_ID, BUYER_ID, CHANNEL_ID, build_env, make_raw_config

_ids = count(1000)
STRANGER = 777


def sub_config(tmp_path, **bot_overrides):
    raw = make_raw_config(tmp_path)
    raw["bots"][0]["products"].append({
        "id": "month",
        "title": "Подписка на месяц",
        "price": {"stars": 300, "crypto": "5"},
        "days": 30,
        "deliver": {"text": "Добро пожаловать в клуб!", "invite_chat_id": CHANNEL_ID},
    })
    raw["bots"][0].update(bot_overrides)
    return raw


@pytest.fixture
async def senv(tmp_path):
    env = await build_env(tmp_path, sub_config(tmp_path))
    yield env
    await env.notifier.close(grace=1)
    await env.db.close()


@pytest.fixture
def dp(senv):
    return build_dispatcher(senv.registry, senv.payments)


async def pay(env, product_id, user_id=BUYER_ID, charge=None):
    product = env.shop.cfg.products[product_id]
    inv = await env.payments.create_invoice(env.shop, method="stars", user_id=user_id, product=product)
    await env.payments.process_stars_payment(
        env.shop, payload=inv.tg_payload, charge_id=charge or f"c-{inv.id}",
        total_amount=int(inv.amount), currency="XTR", user_id=user_id,
    )
    return inv


def texts_to(session, chat_id):
    return [m.text for m in session.calls(SendMessage) if m.chat_id == chat_id]


def kicked(session, user_id):
    return [m for m in session.calls(BanChatMember) if m.user_id == user_id]


# ---------------------------------------------------------------------- выдача и продление


async def test_subscription_purchase_sets_expiry(senv):
    before = int(time.time())
    await pay(senv, "month")
    access = await senv.db.get_access("shop", BUYER_ID, "month")
    assert before + 30 * DAY <= access.expires_at <= int(time.time()) + 30 * DAY
    msgs = texts_to(senv.session(), BUYER_ID)
    assert "Добро пожаловать в клуб!" in msgs[0] and "Доступ действует до" in msgs[0]
    assert senv.session().calls(CreateChatInviteLink)


async def test_renewal_adds_to_remaining_time_and_skips_link_for_members(senv):
    await pay(senv, "month")
    first = (await senv.db.get_access("shop", BUYER_ID, "month")).expires_at
    senv.session().members[(CHANNEL_ID, BUYER_ID)] = "member"  # уже в канале
    await pay(senv, "month")
    second = (await senv.db.get_access("shop", BUYER_ID, "month")).expires_at
    assert second == first + 30 * DAY  # продлили от старой даты, дни не сгорели
    assert len(senv.session().calls(CreateChatInviteLink)) == 1  # повторная ссылка не нужна
    assert "продлён" in texts_to(senv.session(), BUYER_ID)[-1]


async def test_lifetime_product(senv):
    await pay(senv, "vip")
    access = await senv.db.get_access("shop", BUYER_ID, "vip")
    assert access.expires_at is None and access.is_active()


async def test_renewal_after_expiry_starts_from_now(senv):
    await senv.db.save_access(Access("shop", BUYER_ID, "month", expires_at=int(time.time()) - 5 * DAY, revoked=True))
    await pay(senv, "month")
    access = await senv.db.get_access("shop", BUYER_ID, "month")
    assert access.expires_at >= int(time.time()) + 30 * DAY - 5 and not access.revoked


# ---------------------------------------------------------------------- напоминания и окончание


async def test_reminder_sent_once(senv):
    await senv.db.save_access(Access("shop", BUYER_ID, "month", expires_at=int(time.time()) + 2 * DAY))
    await senv.payments.access.process_expirations()
    await senv.payments.access.process_expirations()
    msgs = [m for m in senv.session().calls(SendMessage) if m.chat_id == BUYER_ID]
    assert len(msgs) == 1 and "заканчивается" in msgs[0].text
    assert msgs[0].reply_markup.inline_keyboard[0][0].callback_data == "p:month"


async def test_no_early_reminder_for_far_expiry(senv):
    await senv.db.save_access(Access("shop", BUYER_ID, "month", expires_at=int(time.time()) + 10 * DAY))
    await senv.payments.access.process_expirations()
    assert senv.session().calls(SendMessage) == []


async def test_expired_subscription_kicks_from_channel(senv):
    senv.session().members[(CHANNEL_ID, BUYER_ID)] = "member"
    await senv.db.save_access(Access("shop", BUYER_ID, "month", expires_at=int(time.time()) - 1))
    events = []
    senv.notifier._post = lambda rt, event, data: events.append((event, data))
    await senv.payments.access.process_expirations()
    await senv.notifier.wait_idle()

    assert kicked(senv.session(), BUYER_ID)
    unban = senv.session().calls(UnbanChatMember)[0]
    assert unban.only_if_banned is True  # не баним навсегда — после оплаты сможет вернуться
    assert (await senv.db.get_access("shop", BUYER_ID, "month")).revoked
    msg = [m for m in senv.session().calls(SendMessage) if m.chat_id == BUYER_ID][-1]
    assert "закончился" in msg.text and msg.reply_markup.inline_keyboard[0][0].callback_data == "p:month"
    assert events and events[0][0] == "access_expired" and events[0][1]["access"]["product_id"] == "month"

    await senv.payments.access.process_expirations()  # второй проход ничего не повторяет
    assert len(kicked(senv.session(), BUYER_ID)) == 1


async def test_no_kick_when_other_access_to_same_chat(senv):
    senv.session().members[(CHANNEL_ID, BUYER_ID)] = "member"
    await pay(senv, "vip")  # vip — навсегда, в тот же канал
    await senv.db.save_access(Access("shop", BUYER_ID, "month", expires_at=int(time.time()) - 1))
    await senv.payments.access.process_expirations()
    assert kicked(senv.session(), BUYER_ID) == []


async def test_no_kick_for_admins_and_users_who_left(senv):
    senv.session().members[(CHANNEL_ID, ADMIN_ID)] = "member"
    await senv.db.save_access(Access("shop", ADMIN_ID, "month", expires_at=int(time.time()) - 1))
    await senv.db.save_access(Access("shop", BUYER_ID, "month", expires_at=int(time.time()) - 1))  # уже вышел
    await senv.payments.access.process_expirations()
    assert senv.session().calls(BanChatMember) == []


async def test_kick_failure_alerts_admin(senv):
    senv.session().members[(CHANNEL_ID, BUYER_ID)] = "member"
    senv.session().errors[BanChatMember] = TelegramBadRequest(BanChatMember(chat_id=1, user_id=1), "not enough rights")
    await senv.db.save_access(Access("shop", BUYER_ID, "month", expires_at=int(time.time()) - 1))
    await senv.payments.access.process_expirations()
    assert any("удалить его из чата" in t for t in texts_to(senv.session(), ADMIN_ID))


async def test_refund_revokes_access(senv):
    senv.session().members[(CHANNEL_ID, BUYER_ID)] = "member"
    inv = await pay(senv, "month")
    await senv.payments.refund(await senv.db.get_invoice(inv.id))
    assert not (await senv.db.get_access("shop", BUYER_ID, "month")).is_active()
    assert kicked(senv.session(), BUYER_ID)


# ---------------------------------------------------------------------- охрана канала


def join_request(user_id):
    return Update(update_id=next(_ids), chat_join_request=ChatJoinRequest(
        chat=Chat(id=CHANNEL_ID, type="channel", title="VIP канал"),
        from_user=User(id=user_id, is_bot=False, first_name="u"),
        user_chat_id=user_id, date=datetime.datetime.now(),
    ))


def joined(user_id, by=None, chat_id=CHANNEL_ID, is_bot=False):
    user = User(id=user_id, is_bot=is_bot, first_name="u")
    return Update(update_id=next(_ids), chat_member=ChatMemberUpdated(
        chat=Chat(id=chat_id, type="channel", title="VIP канал"),
        from_user=User(id=by or user_id, is_bot=False, first_name="a"),
        date=datetime.datetime.now(),
        old_chat_member=ChatMemberLeft(user=user),
        new_chat_member=ChatMemberMember(user=user),
    ))


async def feed(env, dp, update):
    await dp.feed_update(env.shop.bot, update)


async def test_join_request_approved_for_payer(senv, dp):
    await pay(senv, "month")
    await feed(senv, dp, join_request(BUYER_ID))
    assert senv.session().calls(ApproveChatJoinRequest)[0].user_id == BUYER_ID
    assert senv.session().calls(DeclineChatJoinRequest) == []


async def test_join_request_declined_for_non_payer(senv, dp):
    await feed(senv, dp, join_request(STRANGER))
    assert senv.session().calls(ApproveChatJoinRequest) == []
    assert senv.session().calls(DeclineChatJoinRequest)[0].user_id == STRANGER
    offer = [m for m in senv.session().calls(SendMessage) if m.chat_id == STRANGER][0]
    assert "оплатил доступ" in offer.text
    assert [r[0].callback_data for r in offer.reply_markup.inline_keyboard] == ["p:vip", "p:month"]


async def test_join_request_left_for_owner_when_guard_off(tmp_path):
    env = await build_env(tmp_path, sub_config(tmp_path, guard=False))
    try:
        dp = build_dispatcher(env.registry, env.payments)
        await feed(env, dp, join_request(STRANGER))
        assert env.session().calls(DeclineChatJoinRequest) == []
        await feed(env, dp, joined(STRANGER))
        assert env.session().calls(BanChatMember) == []
    finally:
        await env.db.close()


async def test_expired_access_does_not_pass_join_request(senv, dp):
    await senv.db.save_access(Access("shop", BUYER_ID, "month", expires_at=int(time.time()) - 1))
    await feed(senv, dp, join_request(BUYER_ID))
    assert senv.session().calls(DeclineChatJoinRequest)


async def test_guard_kicks_stranger_who_joined_by_old_link(senv, dp):
    await feed(senv, dp, joined(STRANGER))
    assert kicked(senv.session(), STRANGER)
    assert any("оплатил доступ" in t for t in texts_to(senv.session(), STRANGER))


async def test_guard_lets_in_payers_admins_bots_and_manual_adds(senv, dp):
    await pay(senv, "month")
    await feed(senv, dp, joined(BUYER_ID))
    await feed(senv, dp, joined(ADMIN_ID))
    await feed(senv, dp, joined(4242, is_bot=True))
    await feed(senv, dp, joined(STRANGER, by=ADMIN_ID))  # админ бота добавил вручную
    await feed(senv, dp, joined(STRANGER, chat_id=-100999))  # чужой чат — не наше дело
    assert senv.session().calls(BanChatMember) == []


# ---------------------------------------------------------------------- админ-команды и витрина


def command(text, uid=ADMIN_ID):
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=datetime.datetime.now(), chat=Chat(id=uid, type="private"),
        from_user=User(id=uid, is_bot=False, first_name="a"), text=text,
    ))


async def test_admin_grant_access_and_revoke(senv, dp):
    await feed(senv, dp, command(f"/grant {STRANGER} month 7"))
    access = await senv.db.get_access("shop", STRANGER, "month")
    assert access.is_active() and access.expires_at <= int(time.time()) + 7 * DAY
    assert "Доступ «Подписка на месяц»" in texts_to(senv.session(), ADMIN_ID)[-1]
    assert senv.session().calls(CreateChatInviteLink)  # пользователю ушла ссылка

    await feed(senv, dp, command(f"/access {STRANGER}"))
    assert "month — ✅ до" in texts_to(senv.session(), ADMIN_ID)[-1]

    senv.session().members[(CHANNEL_ID, STRANGER)] = "member"
    await feed(senv, dp, command(f"/revoke {STRANGER} month"))
    assert "закрыт" in texts_to(senv.session(), ADMIN_ID)[-1]
    assert kicked(senv.session(), STRANGER)


async def test_admin_commands_validate_input(senv, dp):
    await feed(senv, dp, command("/grant abc month"))
    assert "Формат" in texts_to(senv.session(), ADMIN_ID)[-1]
    await feed(senv, dp, command(f"/grant {STRANGER} nope"))
    assert "не найден" in texts_to(senv.session(), ADMIN_ID)[-1]
    # Обычный пользователь не может выдать себе доступ.
    await feed(senv, dp, command(f"/grant {STRANGER} month", uid=STRANGER))
    assert await senv.db.get_access("shop", STRANGER, "month") is None


async def test_purchases_shows_subscriptions(senv, dp):
    await pay(senv, "month")
    await pay(senv, "vip")
    await feed(senv, dp, command("/purchases", uid=BUYER_ID))
    text = texts_to(senv.session(), BUYER_ID)[-1]
    assert "Твои доступы" in text and "Подписка на месяц — до" in text and "VIP — навсегда" in text


async def test_product_card_shows_period(senv, dp):
    from aiogram.methods import EditMessageText

    cb = Update(update_id=next(_ids), callback_query=__import__("aiogram").types.CallbackQuery(
        id="1", from_user=User(id=BUYER_ID, is_bot=False, first_name="b"), chat_instance="c", data="p:month",
        message=Message(message_id=1, date=datetime.datetime.now(), chat=Chat(id=BUYER_ID, type="private"), text="x"),
    ))
    await feed(senv, dp, cb)
    assert "за 30 дн." in senv.session().calls(EditMessageText)[-1].text


async def test_external_bot_gets_one_time_link(senv):
    from platesh.delivery import send_invite

    product = senv.shop.cfg.products["month"]
    await send_invite(senv.ext, BUYER_ID, product)
    link = senv.session("ext").calls(CreateChatInviteLink)[0]
    assert link.member_limit == 1 and not link.creates_join_request


# ---------------------------------------------------------------------- диагностика


async def test_diagnose_reports_problems(senv):
    assert await diagnose(senv.shop) == []
    senv.session().chat_info.update(username="public_channel", has_protected_content=False)
    problems = await diagnose(senv.shop)
    assert any("публичный" in p for p in problems) and any("запрет копирования" in p for p in problems)

    senv.session().members[(CHANNEL_ID, senv.shop.id)] = "member"
    problems = await diagnose(senv.shop)
    assert any("не администратор" in p for p in problems)


async def test_reminder_for_granted_term_on_lifetime_product(senv):
    await senv.db.save_access(Access("shop", BUYER_ID, "vip", expires_at=int(time.time()) + DAY))
    await senv.payments.access.process_expirations()
    assert any("заканчивается" in t for t in texts_to(senv.session(), BUYER_ID))


async def test_short_subscription_reminds_within_third_of_term(tmp_path):
    raw = sub_config(tmp_path)
    raw["bots"][0]["products"][-1]["days"] = 1
    env = await build_env(tmp_path, raw)
    try:
        await env.db.save_access(Access("shop", BUYER_ID, "month", expires_at=int(time.time()) + 10 * 3600))
        await env.payments.access.process_expirations()
        assert env.session().calls(SendMessage) == []  # до конца 10 ч, окно — 8 ч
        await env.db.save_access(Access("shop", BUYER_ID, "month", expires_at=int(time.time()) + 7 * 3600))
        await env.payments.access.process_expirations()
        assert len(env.session().calls(SendMessage)) == 1
    finally:
        await env.db.close()
