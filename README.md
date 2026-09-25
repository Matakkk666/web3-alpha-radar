# Web3 Alpha Radar

Telegram-бот + сканеры X: лист, following смартов и For You. Классификация через Gemini, SQLite, дайджесты в 09:00 и 21:00.

## VPS

```bash
git clone <URL>
cd web3-alpha-radar
nano .env
docker compose run --rm radar python scripts/seed_db.py
docker compose up -d
```

Секреты в `.env` (см. `.env.example`):

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ADMIN_ID=...
GEMINI_API_KEY=...
PROXY_URL=http://user:pass@ip:port
X_AUTH_TOKEN=...
X_LIST_URL=https://x.com/i/lists/...
```

После старта бот пришлёт уведомление в Telegram. Логи: `docker compose logs -f radar`.

Альтернативные имена переменных: `BOT_TOKEN`, `ADMIN_ID`, `AUTH_TOKEN`, `TWITTER_LIST_URL`.
