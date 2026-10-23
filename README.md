# Platesh

Один сервис, который подключает приём оплаты **Telegram Stars ⭐** и **CryptoBot 💎** к любому количеству твоих Telegram-ботов. Добавляешь токены ботов в один конфиг — и все боты принимают платежи, выдают покупки и присылают тебе уведомления.

## Что умеет

- **Несколько ботов сразу.** Все боты из `config.yaml` работают одновременно в одном процессе.
- **Telegram Stars.** Нативные счета Telegram, проверка pre-checkout, возвраты.
- **CryptoBot (Crypto Pay).** USDT, TON, BTC, ETH, LTC, TRX, USDC… Цена — в долларах, рублях и т.д. (покупатель платит любой монетой по курсу) или прямо в монете. Оплата подтверждается опросом API (вебхук не нужен) или вебхуком.
- **Готовая витрина** для ботов без своего кода: каталог, карточка товара, кнопки оплаты, «Проверить оплату», `/purchases`, `/paysupport`, `/terms`, deep-link на товар.
- **Автовыдача покупки:** текст, файл, одноразовая ссылка-приглашение в закрытый канал или чат.
- **Уведомления:** админам в Telegram и POST-запросом на твой сервер (`notify_url`) с HMAC-подписью.
- **HTTP API** для ботов со своим кодом, сайтов и бэкендов. Есть готовый клиент `client/platesh_client.py`.
- **Защита от ошибок:** счёт нельзя оплатить дважды, повторная или «чужая» оплата Stars автоматически возвращается, подпись вебхука CryptoBot проверяется, счёт одного бота нельзя оплатить в другом.
- **Команды админа** в каждом боте: `/stats`, `/payments`, `/invoice ID`, `/refund ID`.
- SQLite, без внешних баз данных. Запуск на Linux, Windows и macOS или в Docker.

## Как это устроено

```
                 ┌──────────────── Platesh ────────────────┐
 бот 1 (shop) ◄──┤ витрина + Stars + CryptoBot (managed)   │
 бот 2 (shop2)◄──┤ витрина + Stars + CryptoBot (managed)   │──► уведомления админам
 бот 3 (свой  ◄──┤ HTTP API: /api/v1/invoices (external)   │──► POST на notify_url
   код)          │ опрос / вебхук CryptoBot                │
                 └─────────────────────────────────────────┘
```

У каждого бота свой режим:

| `mode` | Когда использовать | Что делает Platesh |
|---|---|---|
| `managed` (по умолчанию) | Бот нужен только для продаж, писать код не хочется | Сам запускает бота: каталог, оплата, выдача покупки |
| `external` | У бота уже есть своя логика | Не запускает бота. Создаёт счета по API, принимает оплату и шлёт уведомления |

## Быстрый старт

### 1. Получи токены

- **Бот:** [@BotFather](https://t.me/BotFather) → `/newbot` → токен вида `1234567890:AAH...`. Повтори для каждого бота.
- **CryptoBot:** [@CryptoBot](https://t.me/CryptoBot) → *Crypto Pay* → *Create App* → токен приложения. Для тестов есть [@CryptoTestnetBot](https://t.me/CryptoTestnetBot): в конфиге поставь `testnet: true`.
- **Telegram Stars** работает без дополнительных токенов.
- **Твой Telegram ID** (для уведомлений) можно узнать у [@userinfobot](https://t.me/userinfobot).

### 2. Установи и настрой

Нужен Python 3.10 или новее.

```bash
git clone <этот репозиторий> platesh && cd platesh
cp config.example.yaml config.yaml
cp .env.example .env
```

Впиши токены в `.env`, свой ID в `admins` и товары в `config.yaml` (подробнее — в разделе «Настройка»).

### 3. Запусти

**Linux / macOS:**
```bash
./run.sh check   # проверит токены ботов и CryptoBot
./run.sh         # запуск
```

**Windows:** двойной клик по `run.bat` (или `run.bat check` в консоли).

**Вручную:**
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m platesh check
python -m platesh            # или: python -m platesh -c путь/к/config.yaml
```

**Docker:**
```bash
docker compose up -d --build
docker compose logs -f
```

После запуска **нажми `/start` в каждом боте со своего аккаунта админа**, иначе бот не сможет присылать тебе уведомления.

## Настройка (`config.yaml`)

Полный пример с комментариями — [`config.example.yaml`](config.example.yaml). Секреты лучше держать в `.env` и подставлять как `${ИМЯ}`. Запись `${ИМЯ:-}` значит, что переменная необязательна. Бот с пустым токеном пропускается, так что заготовки в конфиге не мешают.

```yaml
admins: [123456789]

cryptobot:
  token: ${CRYPTOBOT_TOKEN:-}
  currency: USD                 # или RUB, EUR… или монета: USDT, TON
  accepted_assets: [USDT, TON, BTC]

bots:
  - name: shop
    token: ${SHOP_BOT_TOKEN}
    payments: [stars, crypto]
    products:
      - id: vip
        title: VIP-канал на месяц
        description: Доступ в закрытый канал.
        price: { stars: 500, crypto: 9.99 }
        deliver:
          text: "Спасибо за покупку!"
          invite_chat_id: -1001234567890

  - name: other_bot
    token: ${OTHER_BOT_TOKEN}
    payments: [stars]
    products:
      - id: guide
        title: PDF-гайд
        price: { stars: 150 }
        deliver: { file: files/guide.pdf }
```

### Основные параметры бота

| Ключ | Описание |
|---|---|
| `name` | Короткое имя латиницей. Используется в API (`"bot": "shop"`) |
| `token` | Токен от @BotFather |
| `mode` | `managed` или `external` |
| `payments` | Какие способы включены: `[stars, crypto]`, `[stars]` или `[crypto]` |
| `admins` | Дополнительные админы этого бота (к глобальным `admins`) |
| `cryptobot` | Свой токен, валюта или срок счёта CryptoBot для этого бота (иначе берутся глобальные) |
| `notify_url` | Куда отправлять POST об оплатах (иначе глобальный `notify_url`) |
| `welcome`, `support`, `terms` | Приветствие, текст `/paysupport` и `/terms` |
| `texts` | Любые тексты интерфейса, ключи — в [`platesh/texts.py`](platesh/texts.py) |
| `products` | Товары: `id`, `title`, `description`, `price.stars`, `price.crypto`, `deliver` |

### Выдача покупки (`deliver`)

| Ключ | Что происходит после оплаты |
|---|---|
| `text` | Бот отправляет сообщение (поддерживается HTML) |
| `file` | Бот отправляет файл: путь (относительно конфига), `file_id` или URL |
| `invite_chat_id` | Бот создаёт одноразовую ссылку в канал или чат (**бот должен быть там админом** с правом приглашать) |
| `invite_hours` | Сколько часов действует ссылка (по умолчанию 24) |

Можно сочетать все варианты. Если выдать покупку не удалось (например, у бота нет прав в канале), оплата всё равно засчитывается, а админы получают сообщение с ID покупателя.

**Ссылка сразу на товар:** `https://t.me/<бот>?start=p_vip` открывает карточку товара `vip`.

## Бот со своим кодом (`mode: external`)

Platesh не запускает такого бота, поэтому конфликтов с твоим кодом нет. Бот создаёт счета через API, а Platesh отслеживает оплату и сообщает о ней.

1. В `config.yaml` укажи у бота `mode: external` и `notify_url`, а в `server` — `api_key`.
2. Скопируй [`client/platesh_client.py`](client/platesh_client.py) в проект бота.
3. Используй (пример для aiogram 3):

```python
from platesh_client import PlateshClient

platesh = PlateshClient("http://127.0.0.1:8080", api_key="...", bot="my_own_bot")

# Нужен для оплаты Stars: подтверждает платежи через Platesh.
# Счета, созданные не через Platesh, этот роутер не трогает.
dp.include_router(platesh.stars_router())

@router.message(Command("buy"))
async def buy(message: Message):
    # Platesh сам пришлёт пользователю счёт (Stars или кнопку CryptoBot):
    await platesh.create_invoice(method="stars", amount=100, title="Премиум",
                                 user_id=message.from_user.id, payload="premium", send=True)
    # Или получи ссылку и покажи её сам:
    inv = await platesh.create_invoice(method="crypto", amount="4.99", title="Премиум",
                                       user_id=message.from_user.id, payload="premium")
    await message.answer(f"Оплатить: {inv['pay_url']}")
```

Об оплате ты узнаешь из POST на `notify_url` (см. ниже) или проверкой `await platesh.get_invoice(id)`.

> Для Stars обязателен `stars_router()`: Telegram отправляет подтверждение платежа тому, кто получает апдейты бота, то есть твоему коду. Роутер передаёт его в Platesh. Для CryptoBot ничего подключать не нужно.

## HTTP API

Авторизация: заголовок `Authorization: Bearer <api_key>` или `X-API-Key: <api_key>`. Если `server.api_key` не задан, `/api/*` выключен. Ответ всегда `{"ok": true, "result": ...}` или `{"ok": false, "error": "..."}`.

| Метод | Путь | Описание |
|---|---|---|
| `GET` | `/health` | Проверка, что сервис жив (без ключа) |
| `GET` | `/api/v1/bots` | Боты, их режимы, способы оплаты и товары |
| `POST` | `/api/v1/invoices` | Создать счёт |
| `GET` | `/api/v1/invoices/{id}` | Счёт. `?refresh=1` — сразу проверить в CryptoBot |
| `GET` | `/api/v1/invoices?bot=&user_id=&status=&limit=` | Список счетов |
| `POST` | `/api/v1/invoices/{id}/refund` | Вернуть оплату Stars |
| `POST` | `/api/v1/stars/pre-checkout` | Для `external`-ботов (используется клиентом) |
| `POST` | `/api/v1/stars/paid` | Для `external`-ботов (используется клиентом) |
| `POST` | `/webhooks/cryptobot` | Вебхук CryptoBot (проверяется подпись) |

### Создание счёта

```bash
curl -X POST http://127.0.0.1:8080/api/v1/invoices \
  -H "Authorization: Bearer $PLATESH_API_KEY" -H "Content-Type: application/json" \
  -d '{"bot": "shop", "method": "crypto", "product_id": "vip", "user_id": 123456789, "send": true}'
```

| Поле | Описание |
|---|---|
| `bot` | `name` бота из конфига (обязательно) |
| `method` | `stars` или `crypto` (обязательно) |
| `product_id` | Товар из конфига: цена и выдача берутся оттуда |
| `amount` + `title` | Или произвольная сумма (для Stars — целое число) |
| `currency` | Для `crypto`: валюта счёта (по умолчанию из конфига) |
| `description` | Описание счёта |
| `user_id` | Telegram ID покупателя: ему придёт покупка и сообщение «оплата получена» |
| `payload` | Любая твоя строка (например, номер заказа). Вернётся в уведомлении |
| `send` | `true` — Platesh сам отправит счёт пользователю в бота |

Ответ — объект счёта: `id`, `status` (`pending` / `paid` / `expired` / `refunded` / `failed`), `pay_url`, `amount`, `currency`, `paid_asset`, `paid_amount` и др.

## Уведомления на `notify_url`

После оплаты (и возврата) Platesh отправляет POST с JSON. При ошибке он повторяет запрос через 5 с, 30 с, 2 мин и 10 мин.

```json
{
  "event": "invoice_paid",
  "invoice": {"id": "a1b2c3d4e5f6", "bot": "shop", "method": "crypto", "amount": "9.99",
              "currency": "USD", "status": "paid", "user_id": 123456789, "product_id": "vip",
              "paid_asset": "USDT", "paid_amount": "9.99", "payload": "order-42", "...": "..."},
  "bot": {"name": "shop", "id": 1234567890, "username": "my_shop_bot"},
  "user": {"id": 123456789, "username": "buyer", "first_name": "Иван"}
}
```

Заголовки: `X-Platesh-Event: invoice_paid | invoice_refunded` и `X-Platesh-Signature`. Подпись — HMAC-SHA256 от тела запроса с ключом `server.webhook_secret`. Проверка на Python:

```python
from platesh_client import verify_webhook
ok = verify_webhook(raw_body, request.headers.get("X-Platesh-Signature"), WEBHOOK_SECRET)
```

## Вебхук CryptoBot (необязательно)

По умолчанию Platesh каждые `poll_interval` секунд опрашивает CryptoBot. Публичный адрес для этого не нужен. Если у сервера есть HTTPS-домен, можно включить мгновенные уведомления: @CryptoBot → Crypto Pay → My Apps → твоё приложение → Webhooks → `https://твой-домен/webhooks/cryptobot`. Опрос можно оставить как подстраховку.

## Команды в ботах

| Команда | Кто | Что делает |
|---|---|---|
| `/start` | все | Каталог товаров |
| `/purchases` | все | Мои покупки |
| `/paysupport` | все | Помощь с оплатой (Telegram требует её от ботов, принимающих Stars) |
| `/terms` | все | Условия (если заданы `terms`) |
| `/admin` | админы | Список команд админа |
| `/stats` | админы | Выручка за сегодня и за всё время, по этому боту и по всем ботам |
| `/payments` | админы | Последние счета |
| `/invoice ID` | админы | Подробности счёта |
| `/refund ID` | админы | Вернуть оплату Stars |

## Полезно знать

- **Stars** зачисляются на баланс бота. Вывести их можно через Fragment по правилам Telegram. Возврат Stars делается командой `/refund` или через API.
- **CryptoBot** зачисляет крипту на баланс твоего приложения Crypto Pay. Автоматического возврата там нет: возвращай вручную через @CryptoBot.
- Telegram требует продавать **цифровые товары и услуги** внутри ботов за Stars. Учитывай это, когда включаешь крипто-оплату для таких товаров.
- Один токен бота может принимать апдейты только в одном месте. Поэтому бота, у которого уже есть свой код, подключай в режиме `external`.
- Все счета и покупатели хранятся в `data/platesh.db` (SQLite). Делай резервные копии этого файла.

## Разработка

```bash
pip install -r requirements-dev.txt
pytest
```

Тесты подменяют Telegram и CryptoBot и проверяют весь путь: витрину, Stars (pre-checkout, оплату, двойную оплату, возврат), CryptoBot (опрос, вебхук с подписью, истечение счёта), HTTP API, уведомления и клиент для `external`-ботов.

Структура:

```
platesh/
  config.py        загрузка и проверка config.yaml
  payments.py      ядро: счета Stars/CryptoBot, приём оплат, возвраты
  cryptopay.py     клиент Crypto Pay API
  delivery.py      выдача покупки
  notifier.py      уведомления админам и на notify_url
  db.py            SQLite
  web.py           HTTP API и вебхук CryptoBot
  bots/shop.py     витрина и обработчики оплаты
  bots/admin.py    команды админа
  app.py           запуск всего вместе
client/platesh_client.py   клиент для ботов со своим кодом
```
