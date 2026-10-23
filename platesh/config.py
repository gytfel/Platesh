"""Загрузка и проверка config.yaml.

Секреты можно не писать в файл, а брать из окружения: ``token: ${SHOP_BOT_TOKEN}``
или со значением по умолчанию ``${CRYPTOBOT_TOKEN:-}``. Файл ``.env`` рядом с конфигом
подхватывается автоматически (уже заданные переменные окружения не перезаписываются).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from .texts import DEFAULT_TEXTS, format_price

log = logging.getLogger(__name__)

STARS = "stars"
CRYPTO = "crypto"
PAYMENT_METHODS = (STARS, CRYPTO)
MODES = ("managed", "external")

# Фиатные валюты, в которых Crypto Pay умеет выставлять счёт (покупатель платит любой
# криптой из accepted_assets по курсу). Всё остальное считается криптоактивом (USDT, TON...).
FIAT_CURRENCIES = frozenset(
    "USD EUR RUB BYN UAH GBP CNY KZT UZS GEL TRY AMD THB INR BRL IDR AZN AED PLN ILS".split()
)

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class CryptoConfig:
    token: str | None = None
    testnet: bool = False
    currency: str = "USD"
    accepted_assets: tuple[str, ...] = ()
    expires_in: int = 3600

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    @property
    def is_fiat(self) -> bool:
        return self.currency in FIAT_CURRENCIES


@dataclass(frozen=True)
class Delivery:
    text: str | None = None
    file: str | None = None
    invite_chat_id: int | None = None
    invite_hours: int = 24
    protect_content: bool = True


@dataclass(frozen=True)
class Product:
    id: str
    title: str
    description: str = ""
    price_stars: int | None = None
    price_crypto: Decimal | None = None
    delivery: Delivery = field(default_factory=Delivery)
    days: int | None = None  # срок доступа; None — навсегда

    @property
    def chat_id(self) -> int | None:
        return self.delivery.invite_chat_id


@dataclass
class BotConfig:
    name: str
    token: str
    mode: str = "managed"
    admins: tuple[int, ...] = ()
    payments: tuple[str, ...] = PAYMENT_METHODS
    crypto: CryptoConfig = field(default_factory=CryptoConfig)
    products: dict[str, Product] = field(default_factory=dict)
    texts: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TEXTS))
    notify_url: str | None = None
    terms: str | None = None
    guard: bool = True
    api_key: str | None = None

    @property
    def managed(self) -> bool:
        return self.mode == "managed"

    def chats(self) -> set[int]:
        """Закрытые чаты/каналы, доступ в которые продаёт этот бот."""
        return {p.chat_id for p in self.products.values() if p.chat_id is not None}

    def chat_products(self, chat_id: int) -> list[Product]:
        return [p for p in self.products.values() if p.chat_id == chat_id]

    def method_enabled(self, method: str) -> bool:
        if method not in self.payments:
            return False
        return method != CRYPTO or self.crypto.enabled

    def methods_for(self, product: Product) -> list[str]:
        methods = []
        if product.price_stars is not None and self.method_enabled(STARS):
            methods.append(STARS)
        if product.price_crypto is not None and self.method_enabled(CRYPTO):
            methods.append(CRYPTO)
        return methods

    def product_price(self, product: Product) -> str:
        parts = []
        for method in self.methods_for(product):
            if method == STARS:
                parts.append(format_price(product.price_stars, "XTR"))
            else:
                parts.append(format_price(product.price_crypto, self.crypto.currency))
        return " / ".join(parts)

    def text(self, key: str, **kwargs: Any) -> str:
        template = self.texts.get(key, DEFAULT_TEXTS.get(key, key))
        if not kwargs:
            return template
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            log.warning("bots[%s].texts.%s: ошибка в шаблоне, доступные поля: %s", self.name, key, ", ".join(kwargs))
            return template


@dataclass(frozen=True)
class ServerConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    api_key: str | None = None
    webhook_secret: str | None = None
    allowed_ips: tuple[str, ...] = ()


@dataclass
class Config:
    database: Path
    bots: list[BotConfig]
    admins: tuple[int, ...] = ()
    server: ServerConfig = field(default_factory=ServerConfig)
    crypto_poll_interval: float = 10.0
    log_level: str = "INFO"

    def bot(self, name: str) -> BotConfig | None:
        return next((b for b in self.bots if b.name == name), None)


MIN_KEY_LENGTH = 24


def weak_keys(cfg: Config) -> list[str]:
    """Названия слишком коротких ключей API (их проще подобрать)."""
    keys = [("server.api_key", cfg.server.api_key)] + [(f"bots[{b.name}].api_key", b.api_key) for b in cfg.bots]
    return [name for name, key in keys if key and len(key) < MIN_KEY_LENGTH]


# --------------------------------------------------------------------------- helpers


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _expand_env(value: Any, where: str) -> Any:
    if isinstance(value, dict):
        return {k: _expand_env(v, f"{where}.{k}" if where else str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v, f"{where}[{i}]") for i, v in enumerate(value)]
    if not isinstance(value, str):
        return value

    def repl(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        env = os.environ.get(name)
        if env is not None:
            return env
        if default is not None:
            return default
        raise ConfigError(f"{where}: переменная окружения {name} не задана")

    return _ENV_RE.sub(repl, value)


def _section(data: Any, where: str) -> dict[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: ожидается раздел (ключ: значение)")
    return data


def _str(data: dict[str, Any], key: str, where: str, *, required: bool = False, default: str | None = None) -> str | None:
    value = data.get(key)
    if value is None or value == "":
        if required:
            raise ConfigError(f"{where}.{key}: обязательное поле")
        return default
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ConfigError(f"{where}.{key}: ожидается строка")
    return str(value)


def _int(value: Any, where: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{where}: ожидается целое число")
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: ожидается целое число, получено {value!r}") from None
    if minimum is not None and result < minimum:
        raise ConfigError(f"{where}: должно быть не меньше {minimum}")
    return result


def _bool(value: Any, where: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on", "да"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("0", "false", "no", "off", "нет", ""):
        return False
    raise ConfigError(f"{where}: ожидается true/false")


def _decimal(value: Any, where: str) -> Decimal:
    try:
        result = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        raise ConfigError(f"{where}: ожидается число, получено {value!r}") from None
    if not result.is_finite() or result <= 0:
        raise ConfigError(f"{where}: должно быть больше нуля")
    return result


def _id_list(value: Any, where: str) -> tuple[int, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, (int, str)) and not isinstance(value, bool):
        items = [v for v in str(value).replace(",", " ").split() if v]
    elif isinstance(value, list):
        items = value
    else:
        raise ConfigError(f"{where}: ожидается список Telegram ID")
    return tuple(_int(v, f"{where}[{i}]") for i, v in enumerate(items))


def _str_list(value: Any, where: str) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return tuple(v for v in value.replace(",", " ").split() if v)
    if isinstance(value, list):
        return tuple(str(v).strip() for v in value if str(v).strip())
    raise ConfigError(f"{where}: ожидается список")


def _crypto(data: dict[str, Any], where: str, base: CryptoConfig) -> CryptoConfig:
    data = _section(data, where)
    token = _str(data, "token", where, default=base.token)
    currency = (_str(data, "currency", where) or base.currency).upper()
    assets = _str_list(data["accepted_assets"], f"{where}.accepted_assets") if "accepted_assets" in data else base.accepted_assets
    expires_in = _int(data["expires_in"], f"{where}.expires_in", minimum=60) if "expires_in" in data else base.expires_in
    if expires_in > 2_678_400:
        raise ConfigError(f"{where}.expires_in: максимум 2678400 секунд (31 день)")
    testnet = _bool(data["testnet"], f"{where}.testnet") if "testnet" in data else base.testnet
    return CryptoConfig(
        token=token,
        testnet=testnet,
        currency=currency,
        accepted_assets=tuple(a.upper() for a in assets),
        expires_in=expires_in,
    )


def _delivery(data: Any, where: str, base_dir: Path) -> Delivery:
    if isinstance(data, str):
        return Delivery(text=data)
    data = _section(data, where)
    file = _str(data, "file", where)
    if file and not file.startswith(("http://", "https://")):
        candidate = Path(file).expanduser()
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        if candidate.is_file():
            file = str(candidate)
        elif any(ch in file for ch in "/\\."):  # у file_id Telegram таких символов не бывает
            log.warning("%s.file: файл %s не найден — выдача этого товара не сработает", where, candidate)
    invite = data.get("invite_chat_id")
    return Delivery(
        text=_str(data, "text", where),
        file=file,
        invite_chat_id=_int(invite, f"{where}.invite_chat_id") if invite not in (None, "") else None,
        invite_hours=_int(data.get("invite_hours", 24), f"{where}.invite_hours", minimum=1),
        protect_content=_bool(data.get("protect_content", True), f"{where}.protect_content"),
    )


def _product(data: Any, where: str, base_dir: Path) -> Product:
    data = _section(data, where)
    pid = _str(data, "id", where, required=True)
    if not _ID_RE.match(pid):
        raise ConfigError(f"{where}.id: только латиница, цифры, '_' и '-', до 32 символов")
    price = _section(data.get("price"), f"{where}.price")
    stars = price.get("stars")
    crypto = price.get("crypto")
    if stars in (None, "") and crypto in (None, ""):
        raise ConfigError(f"{where}.price: укажи хотя бы одну цену — stars и/или crypto")
    return Product(
        id=pid,
        title=_str(data, "title", where, required=True),
        description=_str(data, "description", where, default=""),
        price_stars=_int(stars, f"{where}.price.stars", minimum=1) if stars not in (None, "") else None,
        price_crypto=_decimal(crypto, f"{where}.price.crypto") if crypto not in (None, "") else None,
        delivery=_delivery(data.get("deliver"), f"{where}.deliver", base_dir),
        days=_int(data["days"], f"{where}.days", minimum=1) if data.get("days") not in (None, "") else None,
    )


def _bot(data: Any, where: str, *, admins: tuple[int, ...], crypto: CryptoConfig, notify_url: str | None, base_dir: Path) -> BotConfig:
    data = _section(data, where)
    name = _str(data, "name", where, required=True)
    if not _ID_RE.match(name):
        raise ConfigError(f"{where}.name: только латиница, цифры, '_' и '-', до 32 символов")
    where = f"bots[{name}]"
    mode = (_str(data, "mode", where) or "managed").lower()
    if mode not in MODES:
        raise ConfigError(f"{where}.mode: допустимо {', '.join(MODES)}")
    payments = tuple(m.lower() for m in _str_list(data.get("payments", list(PAYMENT_METHODS)), f"{where}.payments"))
    unknown = set(payments) - set(PAYMENT_METHODS)
    if unknown or not payments:
        raise ConfigError(f"{where}.payments: допустимо {', '.join(PAYMENT_METHODS)}")

    bot_crypto = _crypto(data.get("cryptobot"), f"{where}.cryptobot", crypto)
    if CRYPTO in payments and not bot_crypto.enabled:
        log.warning("%s: оплата криптой включена, но токен CryptoBot не задан — способ будет скрыт", where)

    products: dict[str, Product] = {}
    raw_products = data.get("products") or []
    if not isinstance(raw_products, list):
        raise ConfigError(f"{where}.products: ожидается список товаров")
    for i, raw in enumerate(raw_products):
        product = _product(raw, f"{where}.products[{i}]", base_dir)
        if product.id in products:
            raise ConfigError(f"{where}.products: повторяется id «{product.id}»")
        products[product.id] = product

    texts = dict(DEFAULT_TEXTS)
    raw_texts = _section(data.get("texts"), f"{where}.texts")
    for key, value in raw_texts.items():
        if key not in DEFAULT_TEXTS:
            log.warning("%s.texts.%s: неизвестный ключ, будет проигнорирован", where, key)
            continue
        texts[key] = "" if value is None else str(value)
    for key in ("welcome", "support"):
        if data.get(key) is not None:
            texts[key] = str(data[key])

    bot_admins = admins + tuple(a for a in _id_list(data.get("admins"), f"{where}.admins") if a not in admins)
    return BotConfig(
        name=name,
        token=_str(data, "token", where, required=True),
        mode=mode,
        admins=bot_admins,
        payments=payments,
        crypto=bot_crypto,
        products=products,
        texts=texts,
        notify_url=_str(data, "notify_url", where, default=notify_url),
        terms=_str(data, "terms", where),
        guard=_bool(data.get("guard", True), f"{where}.guard"),
        api_key=_str(data, "api_key", where),
    )


def parse_config(raw: Any, base_dir: Path) -> Config:
    data = _section(_expand_env(raw, ""), "config")
    admins = _id_list(data.get("admins"), "admins")

    server_data = _section(data.get("server"), "server")
    server = ServerConfig(
        enabled=_bool(server_data.get("enabled", True), "server.enabled"),
        host=_str(server_data, "host", "server", default="0.0.0.0"),
        port=_int(server_data.get("port", 8080), "server.port", minimum=1),
        api_key=_str(server_data, "api_key", "server"),
        webhook_secret=_str(server_data, "webhook_secret", "server"),
        allowed_ips=_str_list(server_data.get("allowed_ips"), "server.allowed_ips"),
    )

    crypto_data = _section(data.get("cryptobot"), "cryptobot")
    crypto = _crypto(crypto_data, "cryptobot", CryptoConfig())
    try:
        poll = float(crypto_data.get("poll_interval", 10) or 0)
    except (TypeError, ValueError):
        raise ConfigError("cryptobot.poll_interval: ожидается число секунд (0 — отключить опрос)") from None

    raw_bots = data.get("bots")
    if not isinstance(raw_bots, list) or not raw_bots:
        raise ConfigError("bots: добавь хотя бы одного бота")
    bots: list[BotConfig] = []
    for i, raw in enumerate(raw_bots):
        if isinstance(raw, dict) and not raw.get("token"):
            log.warning("bots[%s]: токен не задан — бот «%s» пропущен", i, raw.get("name", i))
            continue
        bot = _bot(raw, f"bots[{i}]", admins=admins, crypto=crypto, notify_url=_str(data, "notify_url", "config"), base_dir=base_dir)
        if any(b.name == bot.name for b in bots):
            raise ConfigError(f"bots: повторяется имя «{bot.name}»")
        if any(b.token == bot.token for b in bots):
            raise ConfigError(f"bots[{bot.name}]: этот токен уже используется другим ботом")
        if bot.api_key and (bot.api_key == server.api_key or any(b.api_key == bot.api_key for b in bots)):
            raise ConfigError(f"bots[{bot.name}].api_key: у каждого бота должен быть свой ключ, отличный от server.api_key")
        bots.append(bot)
    if not bots:
        raise ConfigError("bots: ни у одного бота не задан token")

    database = Path(_str(data, "database", "config", default="data/platesh.db")).expanduser()
    if not database.is_absolute():
        database = base_dir / database

    return Config(
        database=database,
        bots=bots,
        admins=admins,
        server=server,
        crypto_poll_interval=poll,
        log_level=(_str(data, "log_level", "config", default="INFO")).upper(),
    )


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"Файл конфигурации не найден: {path}. Скопируй config.example.yaml в config.yaml")
    load_dotenv(path.parent / ".env")
    if Path.cwd() != path.parent:
        load_dotenv(Path.cwd() / ".env")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Ошибка синтаксиса YAML в {path.name}: {exc}") from None
    return parse_config(raw, path.parent)
