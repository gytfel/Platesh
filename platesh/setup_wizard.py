"""Мастер настройки в терминале: python -m platesh setup (или ./run.sh / run.bat при первом запуске).

Задаёт вопросы, проверяет токены, сам создаёт ключи, пишет config.yaml и .env
и подсказывает, что добавить в код ботов.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import socket
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

import yaml

from .net import mask_proxy, normalize_proxy, resolve_proxy

CLIENT_FILE = Path(__file__).resolve().parent.parent / "client" / "platesh_client.py"
DEFAULT_PORT = 8080


class Aborted(Exception):
    pass


# ---------------------------------------------------------------------- проверки токенов (сеть)


TELEGRAM_API = "https://api.telegram.org"
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


def check_bot_token(token: str, proxy: str | None = None, api: str = TELEGRAM_API) -> tuple[str, str]:
    """('ok', username) | ('invalid', причина) | ('offline', причина).

    Обычный HTTP-запрос getMe без библиотеки aiogram: она тяжёлая, и на слабом сервере
    её загрузка ради одной проверки подвешивает машину.
    """
    import aiohttp

    from .net import proxy_connector, proxy_errors

    token = token.strip()
    if not TOKEN_RE.match(token):
        return "invalid", "неверный формат — токен выглядит так: 1234567890:AAH..."

    async def get_me() -> tuple[str, str]:
        timeout = aiohttp.ClientTimeout(total=20)
        connector = proxy_connector(proxy) if proxy else None
        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(f"{api}/bot{token}/getMe") as resp:
                    data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, *proxy_errors()) as exc:
            return "offline", str(exc) or type(exc).__name__
        if isinstance(data, dict) and data.get("ok"):
            result = data.get("result") or {}
            return "ok", result.get("username") or str(result.get("id"))
        if resp.status >= 500:
            return "offline", f"Telegram ответил {resp.status}"
        description = data.get("description") if isinstance(data, dict) else None
        return "invalid", f"Telegram не принял токен ({description or resp.status})"

    return asyncio.run(get_me())


def check_crypto_token(token: str, proxy: str | None = None) -> tuple[str, str, bool]:
    """('ok', имя приложения, testnet) | ('invalid', причина, False) | ('offline', причина, False)."""
    from .cryptopay import CryptoPayClient, CryptoPayError

    async def get_me(testnet: bool) -> tuple[str, str]:
        client = CryptoPayClient(token, testnet=testnet, proxy=proxy)
        try:
            app = await client.get_me()
            return "ok", str(app.get("name") or "приложение")
        except CryptoPayError as exc:
            return ("offline" if exc.code is None else "invalid"), exc.name
        finally:
            await client.close()

    status, info = asyncio.run(get_me(False))
    if status == "invalid":  # может, это токен тестовой сети (@CryptoTestnetBot)
        test_status, test_info = asyncio.run(get_me(True))
        if test_status == "ok":
            return "ok", test_info, True
    return status, info, False


def find_python(bot_dir: Path, *, windows: bool = os.name == "nt") -> str:
    """Чем запускать бота (готовый кусок команды): его виртуальное окружение, если есть, иначе системный Python."""
    for venv in (".venv", "venv", "env"):
        for exe in ("Scripts/python.exe", "bin/python"):
            candidate = bot_dir / venv / exe
            if candidate.is_file():
                return quote(str(candidate))
    if windows:
        found = shutil.which("python")
        # В Windows есть «пустышка» python из WindowsApps — она открывает Microsoft Store, а не запускает код.
        if found and "windowsapps" not in found.lower():
            return "python"
        if shutil.which("py"):
            return "py -3"  # установщик с python.org по умолчанию ставит только py, без python в PATH
    elif shutil.which("python3"):
        return "python3"
    return quote(sys.executable)


def free_port(start: int = DEFAULT_PORT) -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


# ---------------------------------------------------------------------- данные мастера


@dataclass
class BotSetup:
    index: int
    token: str
    username: str | None
    name: str
    mode: str  # external (пейволл) | managed (магазин)
    title: str
    price_stars: int | None
    price_crypto: Decimal | None
    days: int | None
    deliver_text: str | None = None
    script: Path | None = None
    command: str | None = None
    api_key: str = field(default_factory=lambda: secrets.token_urlsafe(32))

    @property
    def token_var(self) -> str:
        return f"BOT{self.index}_TOKEN"

    @property
    def key_var(self) -> str:
        return f"BOT{self.index}_API_KEY"

    @property
    def label(self) -> str:
        return f"@{self.username} ({self.name})" if self.username else self.name


class Wizard:
    def __init__(
        self,
        config_path: Path,
        *,
        ask: Callable[[str], str] = input,
        say: Callable[[str], None] = print,
        check_bot: Callable[[str], tuple[str, str]] = check_bot_token,
        check_crypto: Callable[[str], tuple[str, str, bool]] = check_crypto_token,
    ) -> None:
        self.config_path = config_path
        self.env_path = config_path.parent / ".env"
        self._ask = ask
        self.say = say
        self.check_bot = check_bot
        self.check_crypto = check_crypto
        self.proxy: str | None = None  # что записать в config.yaml (None — auto)
        self.previous: dict = {}  # ответы из прошлой настройки — Enter их оставляет
        self.offline_ok = False  # пользователь решил не проверять токены без связи

    # ------------------------------------------------------------------ ввод

    def ask(self, prompt: str, default: str | None = None) -> str:
        suffix = f" [{default}]" if default not in (None, "") else ""
        try:
            answer = self._ask(f"{prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise Aborted from None
        return answer if answer else (default or "")

    def ask_secret(self, prompt: str, current: str | None) -> str:
        """Токен: если он уже был, Enter оставляет его (в подсказке видны только края)."""
        if not current:
            return self.ask(prompt)
        shown = f"{current[:6]}…{current[-4:]}" if len(current) > 12 else "…"
        answer = self.ask(f"{prompt} [Enter — оставить {shown}]")
        return answer or current

    def load_previous(self) -> dict:
        """Ответы из существующего config.yaml/.env, чтобы при повторной настройке не вводить их снова."""
        try:
            from .config import load_config

            cfg = load_config(self.config_path)
        except Exception:
            return {}
        crypto = next((b.crypto for b in cfg.bots if b.crypto.token), None)
        return {
            "admin": cfg.admins[0] if cfg.admins else None,
            "crypto_token": crypto.token if crypto else None,
            "currency": crypto.currency if crypto else "USD",
            "bot_tokens": [b.token for b in cfg.bots],
            "proxy": cfg.proxy,
        }

    def ask_yes(self, prompt: str, default: bool = True) -> bool:
        while True:
            answer = self.ask(f"{prompt} ({'Д/н' if default else 'д/Н'})").lower()
            if not answer:
                return default
            if answer in ("д", "да", "y", "yes"):
                return True
            if answer in ("н", "нет", "n", "no"):
                return False
            self.say("  Ответь «д» или «н».")

    def ask_int(self, prompt: str, default: int | None = None, *, minimum: int = 0, optional: bool = False) -> int | None:
        while True:
            answer = self.ask(prompt, str(default) if default is not None else None)
            if not answer and optional:
                return None
            if answer.lstrip("-").isdigit() and int(answer) >= minimum:
                return int(answer)
            self.say(f"  Нужно целое число{f' от {minimum}' if minimum else ''}.")

    def ask_price(self, prompt: str) -> Decimal | None:
        while True:
            answer = self.ask(prompt).replace(",", ".")
            if not answer:
                return None
            try:
                value = Decimal(answer)
            except InvalidOperation:
                value = Decimal(0)
            if value.is_finite() and value > 0:
                return value
            self.say("  Нужно число больше нуля, например 4.99.")

    # ------------------------------------------------------------------ шаги

    def ask_crypto(self) -> tuple[str | None, bool, str]:
        self.say("\nCryptoBot — оплата криптой (USDT, TON...). Токен: @CryptoBot → Crypto Pay → Create App.")
        previous = self.previous.get("crypto_token")
        while True:
            if previous:
                token = self.ask_secret("Токен CryptoBot («-» — без крипты)", previous)
                if token == "-":
                    return None, False, "USD"
            else:
                token = self.ask("Токен CryptoBot (Enter — без крипты, только Stars)")
            if not token:
                return None, False, "USD"
            self.say("  Проверяю...")
            status, info, testnet = self.check_crypto(token, resolve_proxy(self.proxy))
            if status == "ok":
                self.say(f"  ✅ Приложение «{info}»{' (тестовая сеть)' if testnet else ''}")
                break
            if status == "offline":
                self.say(f"  ⚠️ Нет связи с CryptoBot ({info}). Сохраню как есть — проверишь потом через check.")
                break
            self.say(f"  ❌ {info}. Попробуй ещё раз.")
        currency = self.ask("Валюта цен для крипты (USD, EUR, RUB или монета USDT, TON)",
                            self.previous.get("currency") or "USD").upper()
        return token, testnet, currency

    def handle_offline(self, token: str, error: str) -> tuple[str, str]:
        """Telegram недоступен: предлагаем VPN или прокси и проверяем снова."""
        self.say(
            f"  ⚠️ Нет связи с Telegram ({error}).\n"
            "  Похоже, api.telegram.org заблокирован у провайдера. Варианты:\n"
            "    • включи VPN в режиме «весь трафик» и нажми Enter — проверю ещё раз;\n"
            "    • или впиши адрес прокси из настроек VPN-клиента, например socks5://127.0.0.1:10808\n"
            "      (v2rayN: 10808, Hiddify: 12334, Clash: 7890 — смотри «локальный порт» в настройках);\n"
            "    • или «-», чтобы сохранить без проверки и разобраться потом."
        )
        answer = self.ask("Прокси / Enter / -")
        if answer == "-":
            self.offline_ok = True
            return "offline", error
        if answer:
            self.proxy = normalize_proxy(answer)
            self.say(f"  Пробую через прокси {mask_proxy(self.proxy)}...")
        else:
            self.say("  Проверяю ещё раз...")
        return self.check_bot(token, resolve_proxy(self.proxy))

    def ask_bot(self, index: int, used_names: set[str], crypto: bool, currency: str) -> BotSetup:
        self.say(f"\n--- Бот №{index} ---")
        old_tokens = self.previous.get("bot_tokens") or []
        while True:
            token = self.ask_secret("Токен бота (от @BotFather)", old_tokens[index - 1] if index <= len(old_tokens) else None)
            if not token:
                self.say("  Токен обязателен.")
                continue
            self.say("  Проверяю...")
            status, info = self.check_bot(token, resolve_proxy(self.proxy))
            while status == "offline" and not self.offline_ok:
                status, info = self.handle_offline(token, info)
            if status == "ok":
                self.say(f"  ✅ @{info}")
                username = info
                break
            if status == "offline":
                self.say("  Сохраню токен без проверки.")
                username = None
                break
            self.say(f"  ❌ {info}. Попробуй ещё раз.")

        name = re.sub(r"[^a-z0-9_-]", "", (username or "").lower())[:32] or f"bot{index}"
        while name in used_names:
            name = f"{name[:29]}_{index}"
        used_names.add(name)

        self.say("Есть ли у этого бота свой код — программа на Python (aiogram 3), которую ты запускаешь?\n"
                 "  нет — Platesh сам ведёт бота: показывает товар, принимает оплату, выдаёт покупку\n"
                 "  да  — оплата встраивается в твой код (пейволл: без оплаты бот не отвечает)")
        mode = "external" if self.ask_yes("У бота есть свой код?", False) else "managed"

        title = self.ask("Название платного доступа" if mode == "external" else "Название товара",
                         "Доступ к боту" if mode == "external" else "Товар")[:64]
        while True:
            stars = self.ask_int("Цена в Telegram Stars (Enter — без Stars)", None, minimum=1, optional=True)
            price_crypto = self.ask_price(f"Цена в {currency} для оплаты криптой (Enter — без крипты)") if crypto else None
            if stars is not None or price_crypto is not None:
                break
            self.say("  Укажи хотя бы одну цену.")
        days = self.ask_int("Срок доступа в днях (0 — навсегда)", 30)
        setup = BotSetup(index=index, token=token, username=username, name=name, mode=mode, title=title,
                         price_stars=stars, price_crypto=price_crypto, days=days or None)

        if mode == "managed":
            setup.deliver_text = self.ask("Что бот напишет после оплаты", "✅ Спасибо за покупку!")
            self.say("  ⚠️ Если у этого бота уже работает свой код — останови его: бот может работать только в одном месте.")
            return setup

        self.say("Platesh может сам запускать код бота — тогда всё работает из одного терминала.")
        while True:
            raw = self.ask("Путь к .py-файлу бота (Enter — буду запускать бота сам)").strip('"\'')
            if not raw:
                self.say("  ⚠️ Пока твой код не запущен, бот не будет отвечать: Platesh сам в нём не отвечает.")
                return setup
            script = Path(raw).expanduser()
            if not script.is_absolute():
                script = (Path.cwd() / script).resolve()
            if script.is_file():
                break
            self.say(f"  Файл не найден: {script}")
        setup.script = script
        default_cmd = f"{find_python(script.parent)} {quote(script.name)}"
        setup.command = self.ask(f"Команда запуска (в папке {script.parent})", default_cmd)
        return setup

    # ------------------------------------------------------------------ запись файлов

    def build_config(self, admin_id: int, port: int, crypto: tuple[str | None, bool, str], bots: list[BotSetup]) -> dict:
        token, testnet, currency = crypto
        config: dict = {
            "database": "data/platesh.db",
            "admins": [admin_id],
            **({"proxy": self.proxy} if self.proxy else {}),
            "server": {
                "host": "127.0.0.1",
                "port": port,
                "api_key": "${PLATESH_API_KEY}",
                "webhook_secret": "${PLATESH_WEBHOOK_SECRET}",
                "allowed_ips": ["127.0.0.1", "::1"],
            },
        }
        if token:
            config["cryptobot"] = {
                "token": "${CRYPTOBOT_TOKEN}",
                "testnet": testnet,
                "currency": currency,
                "accepted_assets": ["USDT", "TON", "BTC", "ETH", "LTC", "TRX", "USDC"],
                "poll_interval": 10,
            }
        config["bots"] = []
        for b in bots:
            price: dict = {}
            if b.price_stars is not None:
                price["stars"] = b.price_stars
            if b.price_crypto is not None:
                price["crypto"] = float(b.price_crypto) if b.price_crypto != b.price_crypto.to_integral_value() else int(b.price_crypto)
            product: dict = {"id": "premium", "title": b.title, "price": price}
            if b.days:
                product["days"] = b.days
            if b.deliver_text:
                product["deliver"] = {"text": b.deliver_text}
            payments = [m for m, p in (("stars", b.price_stars), ("crypto", b.price_crypto)) if p is not None]
            entry: dict = {"name": b.name, "token": f"${{{b.token_var}}}", "mode": b.mode}
            if b.mode == "external":
                entry["api_key"] = f"${{{b.key_var}}}"
            entry["payments"] = payments
            entry["products"] = [product]
            if b.command and b.script:
                entry["run"] = {"command": b.command, "cwd": str(b.script.parent)}
            config["bots"].append(entry)
        return config

    def write_files(self, config: dict, crypto_token: str | None, bots: list[BotSetup]) -> None:
        config_path, env_path = self.config_path, self.env_path
        config_path.parent.mkdir(parents=True, exist_ok=True)
        for path in (config_path, env_path):
            if path.exists():
                shutil.copyfile(path, path.with_name(path.name + ".bak"))
        header = ("# Создано мастером настройки (python -m platesh setup). Можно править вручную —\n"
                  "# описание всех параметров: config.example.yaml и README.md\n\n")
        config_path.write_text(header + yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
        lines = [
            "# Секреты Platesh. Создано мастером настройки — никому не показывай этот файл.",
            f"PLATESH_API_KEY={secrets.token_urlsafe(32)}",
            f"PLATESH_WEBHOOK_SECRET={secrets.token_urlsafe(32)}",
        ]
        if crypto_token:
            lines.append(f"CRYPTOBOT_TOKEN={crypto_token}")
        for b in bots:
            lines.append(f"# {b.label}")
            lines.append(f"{b.token_var}={b.token}")
            if b.mode == "external":
                lines.append(f"{b.key_var}={b.api_key}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def explain(self, bots: list[BotSetup], admin_id: int, port: int) -> None:
        self.say("\n================ Что дальше ================")
        for b in bots:
            self.say(f"\n▶ {b.label}")
            if b.mode == "managed":
                self.say("  Магазин: Platesh ведёт бота сам, ничего добавлять не нужно.")
                continue
            if b.script is not None:
                target = b.script.parent / CLIENT_FILE.name
                if CLIENT_FILE.is_file() and (not target.exists() or target.read_bytes() != CLIENT_FILE.read_bytes()):
                    if self.ask_yes(f"  Скопировать platesh_client.py в {b.script.parent}?"):
                        shutil.copyfile(CLIENT_FILE, target)
                        self.say("  ✅ Скопирован.")
                client_line = "platesh = PlateshClient.from_env()"
                after = "  Бота запустит Platesh — отдельно запускать его не нужно."
            else:
                self.say(f"  Скопируй {CLIENT_FILE} в папку с ботом.")
                client_line = (f'platesh = PlateshClient("http://127.0.0.1:{port}", api_key="{b.api_key}", '
                               f'bot="{b.name}", admins=[{admin_id}])')
                after = "  Запускай бота как обычно, пока работает Platesh."
            self.say("  Добавь в код бота там, где создаётся dp = Dispatcher():\n")
            self.say("      from platesh_client import PlateshClient\n")
            self.say(f"      {client_line}")
            self.say("      dp.include_router(platesh.router())   # раньше своих роутеров")
            self.say("      platesh.install_paywall(dp)\n")
            self.say(after)
        self.say("\nНе забудь нажать /start в каждом боте со своего аккаунта — туда придут уведомления об оплатах.")

    # ------------------------------------------------------------------ сценарий

    def run(self, *, offer_start: bool = True) -> bool:
        """Возвращает True, если пользователь хочет сразу запустить Platesh.

        offer_start=False — не спрашивать про запуск (установка на сервер сама включит службу).
        """
        self.say("=========== Настройка Platesh ===========")
        self.say("Отвечай на вопросы. Enter — значение в [скобках]. Прервать — Ctrl+C.")
        try:
            name = self.config_path.name
            if self.config_path.exists():
                if not self.ask_yes(f"\n{name} уже есть. Настроить заново? (старый сохранится как {name}.bak)", False):
                    return offer_start and self.ask_yes("Запустить Platesh с текущими настройками?")
                self.previous = self.load_previous()
                self.proxy = self.previous.get("proxy")
                if self.previous:
                    self.say("Прошлые ответы подставлены в [скобках] — Enter оставляет их.")
            self.say("\nТвой Telegram ID нужен для уведомлений об оплатах. Узнать: напиши @userinfobot.")
            admin_id = self.ask_int("Твой Telegram ID", self.previous.get("admin"), minimum=1)
            crypto = self.ask_crypto()
            bots: list[BotSetup] = []
            used: set[str] = set()
            tokens: set[str] = set()
            while True:
                bot = self.ask_bot(len(bots) + 1, used, crypto[0] is not None, crypto[2])
                if bot.token in tokens:
                    self.say("  Этот бот уже добавлен — пропускаю.")
                else:
                    tokens.add(bot.token)
                    bots.append(bot)
                if not self.ask_yes("\nДобавить ещё бота?", False):
                    break
            port = free_port()
            if port != DEFAULT_PORT:
                self.say(f"\nПорт {DEFAULT_PORT} занят — API будет на порту {port}.")
            self.write_files(self.build_config(admin_id, port, crypto, bots), crypto[0], bots)
            self.say(f"\n✅ Настройки сохранены в {self.config_path.name} и .env")
            self.explain(bots, admin_id, port)
            return offer_start and self.ask_yes("\nЗапустить Platesh сейчас?")
        except Aborted:
            self.say("\nНастройка прервана.")
            return False
