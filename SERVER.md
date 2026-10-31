# Platesh на сервере: установка, управление, удаление

Здесь собрано всё для работы с Platesh на VPS-сервере. Команды можно копировать целиком.

Архиватор переписки (бот Busnes-tg-perepiski) ставится и подключается по своей инструкции: [SERVER.md в его репозитории](https://github.com/gytfel/Busnes-tg-perepiski/blob/claude/platesh-paywall/SERVER.md). Platesh нужно поставить первым.

**Содержание**

1. [Что нужно](#1-что-нужно)
2. [Установка](#2-установка)
3. [Подключение бота со своим кодом](#3-подключение-бота-со-своим-кодом)
4. [Каждый день: статус, журнал, перезапуск](#4-каждый-день-статус-журнал-перезапуск)
5. [Цены, монеты, товары](#5-цены-монеты-товары)
6. [Доступы и оплаты](#6-доступы-и-оплаты)
7. [Обновление](#7-обновление)
8. [Токены и ключи](#8-токены-и-ключи)
9. [Резервная копия и перенос](#9-резервная-копия-и-перенос)
10. [Удаление](#10-удаление)
11. [Если что-то не работает](#11-если-что-то-не-работает)

---

## 1. Что нужно

- **VPS** с Ubuntu 22.04/24.04 или Debian 12, сервер за границей (там Telegram не блокируется). Хватит 1 ядра и 1 ГБ памяти. Если памяти меньше, сделай файл подкачки (раздел 11).
- **Доступ root.** Подключение из Windows: открой PowerShell и введи `ssh root@IP-АДРЕС`. Пароль при вводе не виден, это нормально.
- **Токены:** бота от @BotFather и, для оплаты криптой, приложения CryptoBot (@CryptoBot → Crypto Pay → Create App).
- **Твой Telegram ID:** напиши @userinfobot.

> Не присылай никому скриншоты с токенами и ключами. Если токен засветился, замени его (раздел 8).

## 2. Установка

Одна команда:

```bash
curl -fsSL https://raw.githubusercontent.com/gytfel/Platesh/refs/heads/claude/beautiful-johnson-i6jquz/install.sh | bash
```

Если зашёл не под root, добавь `sudo`: `... | sudo bash`.

Установщик поставит Python и git, скачает Platesh в `/opt/platesh` и задаст вопросы:

| Вопрос | Что ответить |
|---|---|
| Твой Telegram ID | число от @userinfobot |
| Токен CryptoBot | токен приложения Crypto Pay. Enter — без крипты, только Stars |
| Валюта цен для крипты | `USD`. Тогда покупатель сам выберет монету, а CryptoBot пересчитает сумму по курсу |
| Какими монетами можно платить | например `TON, USDT` |
| Токен бота | от @BotFather. Мастер проверит его |
| У бота есть свой код? | **д**, если у бота своя программа (как у архиватора). **н**, если Platesh сам ведёт бота как магазин |
| Название, цена в Stars, цена в $, срок в днях | срок `0` — навсегда |
| Путь к .py-файлу бота | **Enter**, если бот запускается своей службой (как архиватор) |
| Добавить ещё бота? | **н**, если боты закончились |

В конце установщик включит службу `platesh`. Она работает после закрытия терминала, сама запускается после перезагрузки сервера и перезапускается при сбое.

Повторный запуск той же команды обновляет Platesh. На вопрос «config.yaml уже есть. Настроить заново?» нажми **Enter**, и настройки останутся.

## 3. Подключение бота со своим кодом

Боту со своим кодом нужны адрес Platesh и ключ. Их выдаёт команда `bot-env`. Подставь свой токен вместо `ТОКЕН`:

```bash
cd /opt/platesh && PLATESH_BOT_TOKEN='ТОКЕН' ./run.sh bot-env
```

Она напечатает строки `PLATESH_URL`, `PLATESH_KEY`, `PLATESH_BOT` и `PLATESH_ADMINS`. Их нужно добавить в `.env` бота и перезапустить бота.

**Архиватор подключается одной командой**, она сделает всё сама:

```bash
cd /opt/archiver && bash connect_platesh.sh
```

## 4. Каждый день: статус, журнал, перезапуск

| Команда | Что делает |
|---|---|
| `systemctl status platesh` | Работает ли Platesh (зелёное `active (running)`, выход — `q`) |
| `journalctl -u platesh -f` | Журнал в реальном времени (выход — Ctrl+C) |
| `journalctl -u platesh -n 50 --no-pager` | Последние 50 строк журнала |
| `systemctl restart platesh` | Перезапустить (после изменения настроек) |
| `systemctl stop platesh` | Остановить |
| `systemctl start platesh` | Запустить |
| `systemctl disable --now platesh` | Остановить и убрать из автозапуска |
| `systemctl enable --now platesh` | Вернуть в автозапуск и запустить |
| `cd /opt/platesh && ./run.sh check` | Проверить токены, CryptoBot и монеты (до пары минут на слабом сервере) |

Пока служба работает, не запускай `./run.sh` без команды: получатся два Platesh сразу.

## 5. Цены, монеты, товары

Все настройки лежат в `/opt/platesh/config.yaml`, токены и ключи в `/opt/platesh/.env`. После любого изменения выполни `systemctl restart platesh`. Бот со своим кодом подхватит новые цены сам, в течение 5 минут.

**Цена в Stars.** Команда покажет текущую цену и спросит новую:

```bash
cd /opt/platesh && grep -E "^ +stars:" config.yaml && read -p "Новая цена в Stars: " P && [[ "$P" =~ ^[1-9][0-9]*$ ]] && sed -i -E "s/^( +stars: ).*/\1$P/" config.yaml && systemctl restart platesh && echo "Готово: цена $P Stars" || echo "Не изменено: нужно целое число от 1"
```

Меньше 1 Star Telegram не разрешает.

**Цена в долларах и монеты для крипты.** Поменяй в первой строке `PRICE` (цена в $) и `COINS` (монеты через запятую), потом вставь всё целиком:

```bash
cd /opt/platesh && cp config.yaml config.yaml.bak && PRICE=5 COINS="TON, USDT" .venv/bin/python - <<'EOF'
import os, yaml
c = yaml.safe_load(open("config.yaml", encoding="utf-8"))
coins = [x.strip().upper() for x in os.environ["COINS"].split(",") if x.strip()]
for section in [c.setdefault("cryptobot", {})] + [b["cryptobot"] for b in c["bots"] if "cryptobot" in b]:
    section["currency"], section["accepted_assets"] = "USD", coins
for b in c["bots"]:
    for p in b.get("products", []):
        if (p.get("price") or {}).get("crypto") is not None:
            p["price"]["crypto"] = os.environ["PRICE"]
open("config.yaml", "w", encoding="utf-8").write(yaml.safe_dump(c, allow_unicode=True, sort_keys=False))
print("Крипта:", os.environ["PRICE"], "$, монеты:", ", ".join(coins))
EOF
systemctl restart platesh && ./run.sh check
```

Старые настройки сохранятся в `config.yaml.bak`. В выводе `check` есть строка **«монеты на выбор»**: ✅ у монеты значит, что CryptoBot её принимает, ❌ значит, что монету нужно убрать. Для проверки Platesh создаёт в CryptoBot пробный счёт на каждую монету и сразу его удаляет.

**Название, срок, другие изменения.** Открой файл в редакторе:

```bash
nano /opt/platesh/config.yaml
```

Сохранить: `Ctrl+O`, потом `Enter`. Выйти: `Ctrl+X`. Потом `systemctl restart platesh`. Пример товара:

```yaml
  products:
  - id: premium            # не меняй: по нему хранятся оплаченные доступы
    title: Доступ к боту   # название в счёте Stars (Telegram показывает первые 32 символа)
    price:
      stars: 350           # цена в Telegram Stars
      crypto: '5'          # цена в cryptobot.currency (USD)
    days: 30               # срок; без этой строки — навсегда
```

**Пройти мастер заново** (все вопросы, прошлые ответы подставятся):

```bash
cd /opt/platesh && ./run.sh setup --no-start && systemctl restart platesh
```

Если токен бота тот же, его ключ сохранится, и подключённый бот со своим кодом продолжит работать.

## 6. Доступы и оплаты

Эти команды выполняются в папке `/opt/platesh`, пока служба `platesh` работает. `ID` — Telegram ID пользователя (его можно узнать у @userinfobot).

| Команда | Что делает |
|---|---|
| `./run.sh payments` | Последние 30 оплат: дата, ID, сумма, номер счёта |
| `./run.sh payments ID` | Оплаты одного пользователя |
| `./run.sh access ID` | Есть ли доступ и до какого числа |
| `./run.sh grant ID` | Выдать доступ на срок товара (например, 30 дней) |
| `./run.sh grant ID 7` | Выдать или продлить доступ на 7 дней. Дни добавляются к текущему сроку |
| `./run.sh revoke ID` | Закрыть доступ |
| `./run.sh refund НОМЕР_СЧЁТА` | Вернуть Stars и закрыть доступ. Крипту возвращай вручную через @CryptoBot |

Если ботов несколько, добавь `--bot ИМЯ`, если товаров несколько, `--product ID_ТОВАРА`. Команда сама подскажет, какие есть. Пример:

```bash
cd /opt/platesh && ./run.sh grant 123456789 30
```

Пользователи из списка `admins` в `config.yaml` (твой ID) пользуются ботами без оплаты.

## 7. Обновление

```bash
cd /opt/platesh && ./run.sh update
```

Команда скачает новую версию, поставит новые библиотеки и сама перезапустит службу. Настройки и база не меняются.

Если Platesh ставился раньше 31.10.2026, первое обновление сделай так (настройки и база останутся):

```bash
cd /opt/platesh && git fetch -q origin claude/beautiful-johnson-i6jquz && git reset -q --hard FETCH_HEAD && ./run.sh update
```

Дальше обычная команда будет работать. Эта же команда помогает, если `update` пишет, что файлы программы изменены.

## 8. Токены и ключи

Все секреты лежат в `/opt/platesh/.env`:

| Строка | Что это |
|---|---|
| `BOT1_TOKEN`, `BOT2_TOKEN`… | токены ботов от @BotFather |
| `BOT1_API_KEY`… | ключ, по которому бот со своим кодом обращается к Platesh (`PLATESH_KEY` в `.env` бота) |
| `CRYPTOBOT_TOKEN` | токен приложения CryptoBot |
| `PLATESH_API_KEY` | главный ключ API (им пользуются команды из раздела 6) |
| `PLATESH_WEBHOOK_SECRET` | подпись уведомлений |

**Посмотреть, какие строки есть, не показывая значения:**

```bash
cut -d= -f1 /opt/platesh/.env
```

**Заменить токен бота**, если он засветился:

1. В @BotFather: `/mybots` → бот → API Token → **Revoke current token**. Старый токен сразу перестанет работать.
2. Впиши новый токен в Platesh: `nano /opt/platesh/.env`, строка `BOT1_TOKEN=`, потом `systemctl restart platesh`.
3. Впиши его же в бота со своим кодом. У архиватора это `nano /opt/archiver/.env`, строка `BOT_TOKEN=`, потом `systemctl restart busnes-tg-perepiski`.

**Заменить токен CryptoBot:** в @CryptoBot → Crypto Pay → приложение → API Token → новый токен. Потом впиши его в строку `CRYPTOBOT_TOKEN=` в `/opt/platesh/.env` и выполни `systemctl restart platesh`.

## 9. Резервная копия и перенос

В базе `/opt/platesh/data/platesh.db` записано, кто оплатил и до какого числа. Делай копию хотя бы раз в неделю.

**Сделать копию** (на сервере):

```bash
cd /opt && tar czf /root/platesh-backup-$(date +%F).tgz --ignore-failed-read platesh/config.yaml platesh/.env platesh/data
```

**Скачать копию на компьютер.** Выполни в PowerShell на компьютере, а не на сервере:

```bash
scp root@IP-АДРЕС:/root/platesh-backup-*.tgz .
```

**Восстановить** на чистом сервере: `tar xzf platesh-backup-ДАТА.tgz -C /opt`, потом команда установки из раздела 2. На вопрос «Настроить заново?» нажми **Enter**.

**Перенос на другой сервер** вместе с архиватором описан в [README](README.md#перенос-на-другой-сервер).

## 10. Удаление

Боты со своим кодом, подключённые к Platesh, после его удаления начнут отвечать «Сервис оплаты временно недоступен». Сначала отключи их от оплаты. У архиватора это делает команда из его инструкции (раздел «Сделать бесплатным»).

Удаление Platesh. Первая строка сохраняет копию настроек и базы в `/root`:

```bash
cd /opt && tar czf /root/platesh-backup-$(date +%F).tgz --ignore-failed-read platesh/config.yaml platesh/.env platesh/data
systemctl disable --now platesh
rm -f /etc/systemd/system/platesh.service && systemctl daemon-reload
rm -rf /opt/platesh
```

Копия останется в `/root/platesh-backup-ДАТА.tgz`. Если она не нужна: `rm /root/platesh-backup-*.tgz`.

## 11. Если что-то не работает

| Что видно | Что делать |
|---|---|
| Бот молчит | `journalctl -u platesh -n 50 --no-pager`. У бота со своим кодом смотри и его журнал (у архиватора: `journalctl -u busnes-tg-perepiski -n 50 --no-pager`) |
| В журнале `Conflict: terminated by other getUpdates request` | Этот же бот запущен где-то ещё: на компьютере или на старом сервере. Останови его там |
| «Сервис оплаты временно недоступен» | Platesh не работает: `systemctl status platesh`, потом `systemctl restart platesh` |
| «Оплата в … сейчас недоступна» | CryptoBot не принимает эту монету. Проверь `./run.sh check` и убери монету (раздел 5) |
| `Ошибка в настройках: …` | Опечатка в `config.yaml`. Верни копию: `cd /opt/platesh && cp config.yaml.bak config.yaml && systemctl restart platesh` |
| `No space left on device` | Место на диске кончилось: `df -h /`. Освободить: `journalctl --vacuum-size=100M && apt-get clean` |
| Сервер тормозит, команды зависают | Мало памяти: `free -h`. Если строка `Swap` нулевая, сделай файл подкачки на 1 ГБ: |

```bash
fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

Telegram недоступен с сервера (обычно на серверах в России): смотри раздел [«Telegram недоступен»](README.md#telegram-недоступен-блокировка) в README.
