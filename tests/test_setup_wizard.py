"""Мастер настройки: ответы в терминале → рабочие config.yaml и .env."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from platesh import setup_wizard
from platesh.config import load_config
from platesh.setup_wizard import Wizard


class Terminal:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.output: list[str] = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"мастер спросил лишнее: {prompt}")
        answer = self.answers.pop(0)
        if answer is KeyboardInterrupt:
            raise KeyboardInterrupt
        return answer

    def say(self, text):
        self.output.append(text)

    @property
    def text(self):
        return "\n".join(self.output)


def fake_bot_check(token, proxy=None):
    if token == "bad":
        return "invalid", "Telegram не принял токен"
    if token == "333:offline":
        return "offline", "нет сети"
    if token == "444:blocked":  # Telegram доступен только через прокси
        return ("ok", "proxied_bot") if proxy == "socks5://127.0.0.1:10808" else ("offline", "таймаут")
    return "ok", {"111:AAA": "My_Bot", "222:BBB": "shop_bot"}.get(token, "other_bot")


def fake_crypto_check(token, proxy=None):
    return "ok", "Мой магазин", False


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))  # load_config подхватит .env — не пачкаем окружение тестов


def wizard(tmp_path, answers):
    term = Terminal(answers)
    w = Wizard(tmp_path / "config.yaml", ask=term.ask, say=term.say,
               check_bot=fake_bot_check, check_crypto=fake_crypto_check)
    return w, term


def test_paywall_and_shop_bots(tmp_path):
    bot_dir = tmp_path / "my bot"
    bot_dir.mkdir()
    (bot_dir / "bot.py").write_text("print('hi')\n", encoding="utf-8")
    w, term = wizard(tmp_path, [
        "123456789",                    # мой ID
        "12345:crypto", "",             # CryptoBot, валюта по умолчанию USD
        "ton, usdt; sol",               # монеты на выбор покупателю
        "bad", "111:AAA",               # неверный токен → переспросил
        "д", "", "150", "2,5", "",      # свой код есть (пейволл), название, цены, срок 30
        f'"{bot_dir / "bot.py"}"', "",  # путь (с кавычками, как копирует Windows), команда по умолчанию
        "д",                            # ещё бот
        "222:BBB", "н", "Гайд",         # своего кода нет → магазин
        "", "", "50", "",               # без цен → переспросил, потом 50 Stars
        "0", "",                        # навсегда, текст по умолчанию
        "",                             # больше ботов не надо
        "",                             # скопировать клиент в папку бота — да
        "н",                            # не запускать
    ])
    assert w.run() is False
    assert "Telegram не принял токен" in term.text and "хотя бы одну цену" in term.text

    cfg = load_config(tmp_path / "config.yaml")
    paywall, shop = cfg.bots
    assert cfg.admins == (123456789,)
    assert cfg.server.api_key and len(cfg.server.api_key) >= 32 and cfg.server.allowed_ips == ("127.0.0.1", "::1")
    assert paywall.name == "my_bot" and paywall.mode == "external" and paywall.token == "111:AAA"
    assert paywall.api_key and len(paywall.api_key) >= 32
    premium = paywall.products["premium"]
    assert premium.price_stars == 150 and premium.price_crypto == Decimal("2.5") and premium.days == 30
    assert paywall.crypto.token == "12345:crypto" and paywall.crypto.currency == "USD"
    assert paywall.crypto.accepted_assets == ("TON", "USDT", "SOL")
    assert paywall.run.cwd == bot_dir and paywall.run.command[-1] == "bot.py"

    assert shop.name == "shop_bot" and shop.mode == "managed" and shop.api_key is None and shop.run is None
    guide = shop.products["premium"]
    assert guide.title == "Гайд" and guide.price_stars == 50 and guide.price_crypto is None and guide.days is None
    assert shop.payments == ("stars",)

    # Подсказка для кода бота и копия клиента рядом с ботом.
    assert "PlateshClient.from_env()" in term.text and "install_paywall(dp)" in term.text
    assert (bot_dir / "platesh_client.py").read_bytes() == setup_wizard.CLIENT_FILE.read_bytes()


def test_minimal_stars_only_and_manual_start(tmp_path):
    w, term = wizard(tmp_path, [
        "42", "",                       # ID, без крипты
        "333:offline", "",              # Telegram недоступен; Enter — проверить ещё раз
        "-",                            # всё ещё нет — сохранить без проверки
        "д", "Премиум", "100", "7",
        "",                             # бота запускаю сам
        "н", "д",                       # больше ботов нет, запустить сейчас
    ])
    assert w.run() is True
    cfg = load_config(tmp_path / "config.yaml")
    [bot] = cfg.bots
    assert bot.name == "bot1" and bot.run is None and not bot.crypto.enabled
    assert bot.products["premium"].days == 7
    assert "нет сети" in term.text
    # Бот запускается вручную — в подсказке явные адрес, ключ и имя.
    assert f'api_key="{bot.api_key}"' in term.text and 'bot="bot1"' in term.text and "admins=[42]" in term.text


def test_existing_config_is_backed_up(tmp_path):
    (tmp_path / "config.yaml").write_text("old: true\n", encoding="utf-8")
    (tmp_path / ".env").write_text("OLD=1\n", encoding="utf-8")
    w, _ = wizard(tmp_path, ["д", "1", "", "111:AAA", "", "", "10", "", "", "", "н"])
    w.run()
    assert (tmp_path / "config.yaml.bak").read_text(encoding="utf-8") == "old: true\n"
    assert (tmp_path / ".env.bak").read_text(encoding="utf-8") == "OLD=1\n"
    assert "OLD=1" not in (tmp_path / ".env").read_text(encoding="utf-8")


def test_keep_existing_config(tmp_path):
    (tmp_path / "config.yaml").write_text("old: true\n", encoding="utf-8")
    w, _ = wizard(tmp_path, ["", "д"])  # не перенастраивать, запустить
    assert w.run() is True
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == "old: true\n"


def test_ctrl_c_saves_nothing(tmp_path):
    w, term = wizard(tmp_path, ["1", KeyboardInterrupt])
    assert w.run() is False
    assert not (tmp_path / "config.yaml").exists()
    assert "прервана" in term.text


def test_input_validation(tmp_path):
    w, term = wizard(tmp_path, ["abc", "-5", "77", "", "111:AAA", "", "", "x", "0", "10", "abc", "", "", "", "н"])
    w.run()
    assert term.text.count("Нужно целое число") >= 3
    assert load_config(tmp_path / "config.yaml").bots[0].products["premium"].price_stars == 10


def test_find_python_prefers_bot_venv(tmp_path):
    exe = tmp_path / "my venv" / ".venv" / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert setup_wizard.find_python(tmp_path / "my venv") == f'"{exe}"'  # путь с пробелом — в кавычках


@pytest.mark.parametrize("found, expected", [
    ({"python": r"C:\Python312\python.exe", "py": r"C:\Windows\py.exe"}, "python"),
    # «Пустышка» из Microsoft Store — пропускаем, берём py
    ({"python": r"C:\Users\A\AppData\Local\Microsoft\WindowsApps\python.exe", "py": r"C:\Windows\py.exe"}, "py -3"),
    ({"py": r"C:\Windows\py.exe"}, "py -3"),
])
def test_find_python_on_windows(tmp_path, monkeypatch, found, expected):
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda name: found.get(name))
    assert setup_wizard.find_python(tmp_path, windows=True) == expected


def test_windows_command_is_split_correctly(tmp_path):
    from platesh.config import split_command

    assert split_command('py -3 bot.py') == ("py", "-3", "bot.py")


def test_blocked_telegram_proxy_is_offered_and_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_wizard, "resolve_proxy", lambda value: value)  # без системного прокси
    w, term = wizard(tmp_path, [
        "42", "",
        "444:blocked",
        "127.0.0.1:1",                  # неверный прокси — всё ещё нет связи
        "socks5://127.0.0.1:10808",     # правильный
        "", "", "100", "", "", "н", "н",
    ])
    w.run()
    assert "заблокирован у провайдера" in term.text and "Пробую через прокси" in term.text
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.proxy == "socks5://127.0.0.1:10808"
    assert cfg.bots[0].name == "proxied_bot"


def test_bot_without_code_becomes_shop_by_default(tmp_path):
    w, term = wizard(tmp_path, ["7", "", "111:AAA", "", "VIP", "300", "30", "", "", "н"])
    w.run()
    [bot] = load_config(tmp_path / "config.yaml").bots
    assert bot.managed and bot.products["premium"].price_stars == 300
    assert "Магазин: Platesh ведёт бота сам" in term.text


def test_paywall_without_code_path_warns(tmp_path):
    w, term = wizard(tmp_path, ["7", "", "111:AAA", "д", "", "100", "", "", "н", "н"])
    w.run()
    assert "бот не будет отвечать" in term.text


def test_reconfigure_keeps_previous_tokens_on_enter(tmp_path):
    first, _ = wizard(tmp_path, ["555", "12345:crypto", "EUR", "ton", "111:AAA", "д", "", "100", "", "", "", "", "н"])
    first.run()
    # Второй раз: перенастроить, везде Enter — только режим меняем на магазин.
    second, term = wizard(tmp_path, ["д", "", "", "", "", "", "", "", "100", "", "", "", "", "н"])
    second.run()
    cfg = load_config(tmp_path / "config.yaml")
    [bot] = cfg.bots
    assert cfg.admins == (555,) and bot.token == "111:AAA" and bot.managed
    assert bot.crypto.token == "12345:crypto" and bot.crypto.currency == "EUR" and bot.crypto.accepted_assets == ("TON",)
    assert "оставить 111:AA…" not in term.text  # короткий токен целиком не показываем
    assert "Прошлые ответы подставлены" in term.text


def test_reconfigure_can_drop_crypto(tmp_path):
    wizard(tmp_path, ["555", "12345:crypto", "", "", "111:AAA", "", "", "100", "", "", "", "", "н"])[0].run()
    wizard(tmp_path, ["д", "", "-", "", "", "", "100", "", "", "", "н"])[0].run()
    assert not load_config(tmp_path / "config.yaml").bots[0].crypto.enabled


def test_check_bot_token_without_aiogram(unused_tcp_port):
    """Проверка токена — обычным HTTP-запросом (на слабом сервере aiogram грузится слишком долго)."""
    import threading

    from aiohttp import web

    port = unused_tcp_port

    async def get_me(request):
        token = request.match_info["token"]
        if token == "123456:" + "A" * 35:
            return web.json_response({"ok": True, "result": {"id": 123456, "username": "good_bot"}})
        return web.json_response({"ok": False, "error_code": 401, "description": "Unauthorized"}, status=401)

    app = web.Application()
    app.router.add_get("/bot{token}/getMe", get_me)
    runner = web.AppRunner(app)
    ready = threading.Event()

    def serve():
        import asyncio as aio

        loop = aio.new_event_loop()
        loop.run_until_complete(runner.setup())
        loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", port).start())
        ready.set()
        loop.run_forever()

    threading.Thread(target=serve, daemon=True).start()
    ready.wait(5)
    api = f"http://127.0.0.1:{port}"
    assert setup_wizard.check_bot_token("123456:" + "A" * 35, api=api) == ("ok", "good_bot")
    status, reason = setup_wizard.check_bot_token("123456:" + "B" * 35, api=api)
    assert status == "invalid" and "Unauthorized" in reason
    assert setup_wizard.check_bot_token("плохой", api=api)[0] == "invalid"
    assert setup_wizard.check_bot_token("123456:" + "A" * 35, api="http://127.0.0.1:1")[0] == "offline"


def test_broken_letters_in_answers_are_removed(tmp_path):
    broken = ("Преми".encode() + "у".encode()[:1] + "м".encode()).decode("utf-8", "surrogateescape")
    w, _ = wizard(tmp_path, ["7", "", "111:AAA", "", broken, "300", "30", "", "", "н"])
    w.run()
    [bot] = load_config(tmp_path / "config.yaml").bots
    assert bot.products["premium"].title == "Премим"
    assert "\\u" not in (tmp_path / "config.yaml").read_text(encoding="utf-8")
