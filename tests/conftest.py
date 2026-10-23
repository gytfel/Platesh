from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetBusinessConnection, GetChat, GetChatMember, GetMe, TelegramMethod
from aiogram.types import (
    AcceptedGiftTypes,
    BusinessConnection,
    Chat,
    ChatFullInfo,
    ChatInviteLink,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    Message,
    User,
)

from platesh.bots.registry import BotRegistry, BotRuntime
from platesh.config import parse_config
from platesh.db import Database
from platesh.notifier import Notifier
from platesh.payments import PaymentService

SHOP_TOKEN = "111111:shop-token"
EXT_TOKEN = "222222:external-token"
CRYPTO_TOKEN = "12345:crypto-token"
ADMIN_ID = 1000
BUYER_ID = 555
CHANNEL_ID = -100123


def chat_member(status: str, user_id: int, **rights: bool):
    user = User(id=user_id, is_bot=False, first_name="u")
    if status == "administrator":
        flags = {name: True for name in (
            "can_be_edited", "can_manage_chat", "can_delete_messages", "can_manage_video_chats",
            "can_restrict_members", "can_promote_members", "can_change_info", "can_invite_users",
            "can_post_stories", "can_edit_stories", "can_delete_stories", "can_send_welcome_messages",
        )}
        flags.update(rights)
        return ChatMemberAdministrator(user=user, is_anonymous=False, **flags)
    if status == "creator":
        return ChatMemberOwner(user=user, is_anonymous=False)
    if status == "member":
        return ChatMemberMember(user=user)
    return ChatMemberLeft(user=user)


class MockSession(BaseSession):
    """Сессия aiogram без сети: запоминает вызовы и возвращает правдоподобные ответы."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.errors: dict[type, Exception] = {}
        # Участники чатов: (chat_id, user_id) -> статус. Сам бот по умолчанию — админ со всеми правами.
        self.members: dict[tuple[int, int], Any] = {}
        self.chat_info: dict[str, Any] = {"title": "VIP канал", "has_protected_content": True}
        self.business: dict[str, int] = {}  # business_connection_id -> владелец аккаунта

    async def close(self) -> None:
        pass

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.requests.append(method)
        if type(method) in self.errors:
            raise self.errors[type(method)]
        if isinstance(method, GetChatMember):
            member = self.members.get((method.chat_id, method.user_id))
            if member is None:
                member = "administrator" if method.user_id == bot.id else "left"
            return chat_member(member, method.user_id) if isinstance(member, str) else member
        if isinstance(method, GetBusinessConnection):
            owner = self.business[method.business_connection_id]
            return BusinessConnection(
                id=method.business_connection_id, user=User(id=owner, is_bot=False, first_name="owner"),
                user_chat_id=owner, date=datetime.datetime.now(), is_enabled=True,
            )
        if isinstance(method, GetChat):
            return ChatFullInfo(
                id=method.chat_id, type="channel", accent_color_id=0, max_reaction_count=11,
                accepted_gift_types=AcceptedGiftTypes(
                    unlimited_gifts=False, limited_gifts=False, unique_gifts=False, premium_subscription=False,
                    gifts_from_channels=False,
                ),
                **self.chat_info,
            )
        returning = method.__returning__
        if returning is Message:
            chat_id = getattr(method, "chat_id", 0)
            return Message(message_id=len(self.requests), date=datetime.datetime.now(), chat=Chat(id=chat_id, type="private"))
        if returning is bool:
            return True
        if returning is str:
            return "https://t.me/$invoice-link"
        if returning is ChatInviteLink:
            return ChatInviteLink(
                invite_link="https://t.me/+one-time", creator=User(id=bot.id, is_bot=True, first_name="bot"),
                creates_join_request=False, is_primary=False, is_revoked=False,
            )
        if isinstance(method, GetMe):
            return User(id=bot.id, is_bot=True, first_name="bot", username=f"bot{bot.id}")
        return True

    def calls(self, method_type: type) -> list[Any]:
        return [r for r in self.requests if isinstance(r, method_type)]


class FakeCrypto:
    """Подмена CryptoPayClient."""

    instances: list[FakeCrypto] = []

    def __init__(self, token: str, *, testnet: bool = False, proxy: str | None = None) -> None:
        self.token = token
        self.testnet = testnet
        self.proxy = proxy
        self.invoices: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self.fail: Exception | None = None
        FakeCrypto.instances.append(self)

    async def create_invoice(self, amount: Any, **kwargs: Any) -> dict[str, Any]:
        if self.fail:
            raise self.fail
        invoice_id = 900 + len(self.created)
        self.created.append({"amount": amount, **kwargs})
        item = {
            "invoice_id": invoice_id,
            "hash": f"IV{invoice_id}",
            "status": "active",
            "bot_invoice_url": f"https://t.me/CryptoBot?start=IV{invoice_id}",
            "payload": kwargs.get("payload"),
        }
        self.invoices[str(invoice_id)] = item
        return dict(item)

    async def get_invoices(self, invoice_ids: list[Any]) -> list[dict[str, Any]]:
        return [dict(self.invoices[str(i)]) for i in invoice_ids if str(i) in self.invoices]

    def set_status(self, invoice_id: str, status: str, **extra: Any) -> dict[str, Any]:
        self.invoices[invoice_id].update(status=status, **extra)
        return dict(self.invoices[invoice_id])

    async def close(self) -> None:
        pass


def make_raw_config(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "database": str(tmp_path / "test.db"),
        "admins": [ADMIN_ID],
        "server": {"api_key": "secret-key", "webhook_secret": "hook-secret"},
        "cryptobot": {"token": CRYPTO_TOKEN, "currency": "USD", "accepted_assets": ["USDT", "TON"]},
        "bots": [
            {
                "name": "shop",
                "token": SHOP_TOKEN,
                "products": [
                    {
                        "id": "vip",
                        "title": "VIP",
                        "description": "Доступ в канал",
                        "price": {"stars": 100, "crypto": "4.99"},
                        "deliver": {"text": "Добро пожаловать!", "invite_chat_id": CHANNEL_ID},
                    },
                    {"id": "stars_only", "title": "Гайд", "price": {"stars": 50}, "deliver": "Держи гайд"},
                ],
            },
            {"name": "ext", "token": EXT_TOKEN, "mode": "external", "notify_url": "http://example.invalid/hook"},
        ],
    }
    raw.update(overrides)
    return raw


def make_bot(token: str) -> Bot:
    return Bot(token, session=MockSession(), default=DefaultBotProperties(parse_mode="HTML"))


class Env:
    def __init__(self, cfg, db, registry, notifier, payments) -> None:
        self.cfg = cfg
        self.db = db
        self.registry = registry
        self.notifier = notifier
        self.payments = payments

    @property
    def shop(self) -> BotRuntime:
        return self.registry.get("shop")

    @property
    def ext(self) -> BotRuntime:
        return self.registry.get("ext")

    def session(self, name: str = "shop") -> MockSession:
        return self.registry.get(name).bot.session  # type: ignore[return-value]

    def crypto(self) -> FakeCrypto:
        return self.payments.crypto(self.shop)  # type: ignore[return-value]


async def build_env(tmp_path: Path, raw: dict[str, Any] | None = None) -> Env:
    cfg = parse_config(raw or make_raw_config(tmp_path), tmp_path)
    db = await Database.open(cfg.database)
    runtimes = []
    for b in cfg.bots:
        bot = make_bot(b.token)
        runtimes.append(BotRuntime(cfg=b, bot=bot, id=bot.id, username=f"{b.name}_bot"))
    registry = BotRegistry(runtimes)
    notifier = Notifier(cfg, db)
    notifier.retry_delays = (0,)
    payments = PaymentService(cfg, db, registry, notifier, crypto_factory=FakeCrypto)
    return Env(cfg, db, registry, notifier, payments)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncGenerator[Env, None]:
    e = await build_env(tmp_path)
    yield e
    await e.notifier.close(grace=1)
    await e.db.close()
