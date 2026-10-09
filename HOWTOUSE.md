# HOWTOUSE — NewsBot на VPS

Памятка оператора: как поднять сервис на VPS, управлять им и остановить. Все команды выполняются в SSH-сессии на сервере. Репозиторий лежит в `/root/newsbot`, данные — в Docker volume.

---

## 1. Подключение

```bash
ssh root@vac9          # или ssh -i /mnt/d/keys/vpnkey root@194.62.251.192
cd /root/newsbot
```

---

## 2. Один раз: конфигурация `.env`

Секреты и настройки хранятся в `/root/newsbot/.env` (не коммитится). Проверь, что заполнены:

| Переменная | Назначение |
|---|---|
| `GROQ_API_KEY` | AI-оценка, заголовки, Vision-подбор фото |
| `FILTER_MODEL` | модель для анализа и заголовков (по умолч. `openai/gpt-oss-120b`) |
| `VISION_MODEL` | Vision для выбора фото (по умолч. `qwen/qwen3.8-27b`) |
| `BOT_TOKEN` | токен Telegram-бота редактора |
| `TELEGRAM_CHANNEL_ID` | канал публикации |
| `TELEGRAM_REVIEW_CHAT_ID` | приватный чат редакции (approve/skip через бота) |
| `NEWSBOT_AUTOPUBLISH_ENABLED` | `true` — автономный режим (автопубликация одобренных) |
| `NEWSBOT_POLL_SECONDS` | интервал опроса RSS (по умолч. `3600`) |

Настройки редакции (пороги интересности, лимиты, список публикуемых турниров) — в `/root/newsbot/editorial.yaml`.

---

## 3. Запуск сервиса

```bash
cd /root/newsbot
git pull --ff-only origin main
docker compose config --quiet
docker compose up -d --build
```

`up -d --build` собирает текущий код, запускает контейнер `newsbot-app` в фоне и пересоздаёт его при изменении образа. Сервис опрашивает RSS по расписанию (раз в час), анализирует новости, шлёт редакционные уведомления и (в автономном режиме) публикует одобренные события.

Проверить, что поднялся:

```bash
docker compose ps
docker compose logs --tail=50 app
```

---

## 4. Один раз: импорт фонов, эмблем и флагов

Каталог `/root/newsbot/assets/logos` смонтирован в контейнер как `/logos` (только чтение). Это локальная коллекция FCLOGO: эмблемы клубов (`logos/<федерация>/clubs/<клуб>/`), флаги стран (`countries/countries/flags.yaml`) и фоны (`background/`).

Импортировать эмблемы клубов и флаги стран в библиотеку (идемпотентно, дедупликация по SHA‑256):

```bash
docker compose run --rm app python -m newsbot.assets --import-fclogo /logos
```

Импортировать фоны карточек (лицензия/основание обязательны):

```bash
docker compose run --rm app python -m newsbot.assets \
  --import-backgrounds /logos/background \
  --license "© D.V.V FM LIVE - 2026. All rights reserved."
```

Проверить библиотеку:

```bash
docker compose run --rm app python -m newsbot.assets --list
```

Эмблемы/флаги подбираются по названию команды: на карточке результата матча клубный герб (иначе флаг страны, иначе инициалы). Добавить одиночный герб вручную:

```bash
docker compose run --rm app python -m newsbot.assets \
  --add /imports/zenit.png --type crest --team-key zenit \
  --name "Зенит" --aliases "Зенит;Зенит Санкт-Петербург;Зенит СПб" \
  --license "<основание использования>"
```

---

## 5. Автономный режим (автопубликация)

Включи в `.env`:

```
NEWSBOT_AUTOPUBLISH_ENABLED=true
```

и пересоздай контейнер (`docker compose up -d`). В автономном режиме цикл сбора сам, после анализа, публикует события в состоянии `approved`:

- `interesting_news` — не более `interesting_news.daily_limit` (по умолч. 2) в сутки по Москве;
- `match_results` — завершённые матчи из выбранных соревнований.

Одобрение всё равно осознанное: событие попадает в `approved` либо командой `--approve`, либо через редакционного бота в `TELEGRAM_REVIEW_CHAT_ID`. Автопубликация не обходит фактологическую проверку и ручное одобрение.

---

## 6. Управление очередью

Показать события по состоянию:

```bash
docker compose run --rm app python -m newsbot.workflow --state awaiting_review
docker compose run --rm app python -m newsbot.workflow --state awaiting_verification
docker compose run --rm app python -m newsbot.workflow --state approved
docker compose run --rm app python -m newsbot.workflow --state published
docker compose run --rm app python -m newsbot.workflow --state skipped
```

Состояния:

- `awaiting_verification` — ждём AI-оценку или независимый источник;
- `awaiting_review` — прошло пороги, нужна редакторская проверка;
- `approved` — разрешено к публикации (автопубликация заберёт сама);
- `publishing` / `retry_wait` / `published` / `skipped` / `failed`.

Вручную обработать партию ожидающих (нужен `GROQ_API_KEY`):

```bash
docker compose run --rm app python -m newsbot.workflow --analyze
```

Одобрить / пропустить:

```bash
docker compose run --rm app python -m newsbot.workflow --approve 123 --priority routine
docker compose run --rm app python -m newsbot.workflow --skip 123 --reason "Причина"
```

Предпросмотр поста без отправки:

```bash
docker compose run --rm app python -m newsbot.workflow --preview 123
```

Опубликовать вручную (вне автономного режима):

```bash
docker compose run --rm app python -m newsbot.workflow --publish 123
```

---

## 7. Логи и диагностика

```bash
docker compose logs --tail=100 app
docker compose logs -f --tail=100 app     # следить; выход — Ctrl+C
docker stats newsbot-app
```

Полезные запросы к базе (из контейнера):

```bash
docker compose run --rm app python -c "import sqlite3; db=sqlite3.connect('/data/newsbot.sqlite3'); db.row_factory=sqlite3.Row; [print(dict(r)) for r in db.execute(\"SELECT state, COUNT(*) c FROM events GROUP BY state\")]"
```

---

## 8. Остановка и перезапуск

```bash
docker compose stop app        # остановить, данные сохраняются
docker compose start app       # запустить снова
docker compose restart app     # перезапустить (начнёт новый цикл сбора)
docker compose down            # остановить проект (volume остаётся)
```

**Не используй `docker compose down -v`** — флаг `-v` удалит постоянную базу и медиа-библиотеку.

---

## 9. Резервная копия

```bash
mkdir -p /root/newsbot-backups
docker compose run --rm -v /root/newsbot-backups:/backups app python -c \
  "import sqlite3,datetime; s=sqlite3.connect('/data/newsbot.sqlite3'); d=sqlite3.connect('/backups/newsbot-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'.sqlite3'); s.backup(d); d.close(); s.close()"
ls -lh /root/newsbot-backups
```

---

## 10. Устранение неполадок

**События застряли в `config_error`** (например, после временного `Groq HTTP 403`): очередь не закрывается до следующего `--analyze`. Перезапусти анализ:

```bash
docker compose run --rm app python -c "import sqlite3; db=sqlite3.connect('/data/newsbot.sqlite3'); db.execute(\"UPDATE events SET ai_state='pending', ai_next_attempt_at=NULL, ai_reason=NULL, ai_attempts=0, draft_text=NULL, analyzed_at=NULL, headline_text=NULL, headline_generated_at=NULL WHERE state IN ('awaiting_verification','awaiting_review','retry_wait') AND ai_state IN ('config_error','retry_wait','complete')\"); db.commit(); print('re-queued')"
docker compose run --rm app python -m newsbot.workflow --analyze
```

**Карточки без фото**: подбор идёт по цепочке «медиа из RSS → Wikimedia Commons по субъекту → AI-фото → фон+заголовок». Если RSS-источники не разрешают переиспользование медиа, работает Commons (свободные лицензии CC0 / PD / GFDL / CC‑BY / CC‑BY‑SA / CC‑BY‑NC). AI-фото включается отдельно переменными `IMAGE_GEN_API_KEY` / `IMAGE_GEN_MODEL` / `IMAGE_GEN_BASE_URL` (провайдер ещё не финализирован). Отсутствие фото не блокирует публикацию.

**Пустая очередь `awaiting_review`** — это не сбой: могло не быть новостей, прошедших пороги (`relevance`, `interest_score >= 7` для новостей).