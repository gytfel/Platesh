"""Токен бота в одном месте: python -m platesh token-from /opt/archiver/.env

Боту со своим кодом токен всё равно нужен в его .env. Команда делает так, чтобы Platesh читал токен
оттуда же (token_from в config.yaml), и убирает копию токена из .env Platesh. Сменил токен в
@BotFather — меняешь его только в .env бота и перезапускаешь обе службы.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from .config import _ENV_RE, TOKEN_VAR, ConfigError, load_config, read_dotenv


class TokenSourceError(Exception):
    pass


def _header(text: str) -> str:
    """Комментарии в начале файла — сохраняем их при перезаписи."""
    lines = []
    for line in text.splitlines(keepends=True):
        if not line.startswith("#") and line.strip():
            break
        lines.append(line)
    return "".join(lines)


def use_token_from(config_path: Path, env_file: Path, *, bot_name: str | None = None, var: str = TOKEN_VAR) -> str:
    config_path, env_file = config_path.resolve(), env_file.expanduser().resolve()
    if not env_file.is_file():
        raise TokenSourceError(f"нет файла {env_file}")
    token = read_dotenv(env_file).get(var, "").strip()
    if not token:
        raise TokenSourceError(f"в {env_file} нет строки {var}=")
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        raise TokenSourceError(f"ошибка в настройках Platesh: {exc}") from None
    bot = cfg.bot(bot_name) if bot_name else next((b for b in cfg.bots if b.token == token), None)
    if bot is None:
        raise TokenSourceError(
            f"бота «{bot_name}» нет в Platesh" if bot_name
            else f"в Platesh нет бота с токеном из {env_file} — токены отличаются. "
                 "Впиши одинаковый токен или укажи бота: --bot ИМЯ"
        )
    if bot.token_from == env_file:
        return f"Токен бота «{bot.name}» уже берётся из {env_file}"

    env_path = config_path.parent / ".env"
    text = config_path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    entry = next((b for b in raw.get("bots") or [] if isinstance(b, dict) and b.get("name") == bot.name), None)
    if entry is None:
        raise TokenSourceError(f"не нашёл бота «{bot.name}» в {config_path.name}")
    old = str(entry.pop("token", "") or "")
    entry.pop("token_var", None)
    # token_from — сразу за name, чтобы было видно в файле.
    items = list(entry.items())
    entry.clear()
    for key, value in items:
        entry[key] = value
        if key == "name":
            entry["token_from"] = str(env_file)
            if var != TOKEN_VAR:
                entry["token_var"] = var
    new_text = _header(text) + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)

    # Переменные вроде BOT1_TOKEN, на которые больше никто не ссылается, — из .env Platesh убираем.
    unused = [name for name, _ in _ENV_RE.findall(old) if f"${{{name}" not in new_text]
    backups = []
    for path in (config_path, env_path):
        if path.is_file():
            backup = path.with_name(path.name + ".bak")
            shutil.copyfile(path, backup)
            backups.append((backup, path))
    try:
        config_path.write_text(new_text, encoding="utf-8")
        removed = []
        if unused and env_path.is_file():
            kept = []
            for line in env_path.read_text(encoding="utf-8").splitlines():
                name = line.split("=", 1)[0].strip().removeprefix("export ").strip()
                if "=" in line and not line.lstrip().startswith("#") and name in unused:
                    removed.append(name)
                else:
                    kept.append(line)
            env_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        check = load_config(config_path).bot(bot.name)
        if check is None or check.token != token:
            raise TokenSourceError("после изменения токен не совпал")
    except Exception as exc:
        for backup, path in backups:
            shutil.copyfile(backup, path)
        if isinstance(exc, TokenSourceError):
            raise
        raise TokenSourceError(f"не получилось, настройки возвращены как были: {exc}") from None
    note = f" Из {env_path} убрана копия: {', '.join(removed)}." if removed else ""
    return (f"✅ Токен бота «{bot.name}» теперь хранится только в {env_file} (строка {var}=).{note}\n"
            f"   Сменишь токен — поменяй его там и перезапусти обе службы.")
