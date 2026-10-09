# Памятка: NewsBot на VPS

Команды ниже выполняются в SSH-сессии на VPS. В примерах репозиторий находится в `/root/newsbot`, а ID события заменяется числом из вывода очереди.

## Быстрый старт и обновление

```bash
cd /newsbot
git status -sb
git pull --ff-only origin main
docker compose config --quiet
docker compose up -d --build app
```

`up -d --build` собирает текущий код, запускает сервис в фоне и пересоздаёт контейнер, если образ изменился. Сервис сам опрашивает RSS по расписанию из `.env` (по умолчанию раз в час). Автопубликация сейчас выключена.

Остановить и снова запустить сервис:

```bash
docker compose stop app
docker compose start app
```

Перезапустить контейнер (он сразу начнёт новый цикл сбора):

```bash
docker compose restart app
```

Посмотреть состояние, логи и использование ресурсов:

```bash
docker compose ps
docker compose logs --tail=100 app
docker compose logs -f --tail=100 app
docker stats newsbot-app
```

В режиме просмотра логов нажми `Ctrl+C`, чтобы выйти из просмотра. Сам сервис продолжит работать.

## Один раз собрать RSS или запустить workflow

Одноразовый сбор RSS и анализ доступной партии:

```bash
docker compose run --rm app python -m newsbot.main --once
```

Команда завершает контейнер после одного цикла. Данные SQLite остаются в постоянном томе. В текущей реализации одноразовый цикл отправляет запросы к активным лентам подряд; для регулярной работы используй сервис через `docker compose up -d app`, который распределяет запросы по часу.

Все команды очереди ниже выполняются отдельным временным контейнером, поэтому подходят и тогда, когда сервис остановлен:

```bash
docker compose run --rm app python -m newsbot.workflow --help
```

Показать события по состоянию:

```bash
docker compose run --rm app python -m newsbot.workflow --state awaiting_review
docker compose run --rm app python -m newsbot.workflow --state awaiting_verification
docker compose run --rm app python -m newsbot.workflow --state approved
docker compose run --rm app python -m newsbot.workflow --state retry_wait
docker compose run --rm app python -m newsbot.workflow --state published
docker compose run --rm app python -m newsbot.workflow --state skipped
docker compose run --rm app python -m newsbot.workflow --state failed
```

- `awaiting_review`: ИИ подтвердил футбольную релевантность, оценка интересности (новость) или корректный счёт (матч) прошли порог, и набралось нужное число независимых подтверждений; нужна редакторская проверка.
- `awaiting_verification`: ждём AI-оценку, независимый источник или ручную проверку.
- `retry_wait`: система ждёт повторной попытки; причина и срок указаны в карточке события.
- `approved`: редактор разрешил публикацию.
- `publishing`: проверяй канал перед повтором команды — ответ Telegram мог потеряться после фактической отправки.
- `published`, `skipped`, `failed`: конечные состояния.

Пустая очередь `awaiting_review` не означает сбой: пока может не быть новостей, прошедших все критерии.

## AI-оценка и ручная проверка

Вручную обработать следующую партию ожидающих событий (нужен `GROQ_API_KEY` в серверном `.env`):

```bash
docker compose run --rm app python -m newsbot.workflow --analyze
```

Партия — до 15 событий. Команда не собирает RSS повторно и не публикует посты.

Показать кандидатов после анализа:

```bash
docker compose run --rm app python -m newsbot.workflow --state awaiting_review
```

Подставь ID события вместо `123`. Одобрение требует завершённой AI-оценки (`ai_state=complete`) и футбольной релевантности. Для новости — оценка интересности не ниже порога `min_interest_score` (сейчас 7/10) и хотя бы один доверенный издатель; для результата матча — подтверждённый счёт из выбранных соревнований и два независимых издателя. Пороги задаются в `editorial.yaml`. Приоритет — отдельная метка:

```bash
docker compose run --rm app python -m newsbot.workflow --approve 123 --priority routine
docker compose run --rm app python -m newsbot.workflow --approve 123 --priority major_match
```

Для проверенного заявления организации от её собственного официального источника можно использовать исключение по подтверждениям:

```bash
docker compose run --rm app python -m newsbot.workflow --approve 123 --official-statement
```

Это не отменяет AI-проверку, футбольную релевантность и порог важности.

Пропустить событие можно только с причиной:

```bash
docker compose run --rm app python -m newsbot.workflow --skip 123 --reason "Причина пропуска"
```

Предпросмотр без публикации и без отправки сообщения в канал:

```bash
docker compose run --rm app python -m newsbot.workflow --preview 123
```

Для новости подобрать иллюстрацию к посту (событие должно быть одобрено; нужен Groq Vision). Подбор идёт по цепочке фолбэков: медиа из RSS → фото Wikimedia Commons по главному субъекту новости (свободные лицензии CC0 / Public Domain / GFDL / CC-BY / CC-BY-SA / CC-BY-NC, без -ND) → AI-фото:

```bash
docker compose run --rm app python -m newsbot.workflow --select-image 123
```

Пересобрать карточку завершённого матча и проверить дизайн через Vision:

```bash
docker compose run --rm app python -m newsbot.workflow --review-scorecard 123
```

Опубликовать одобренное событие вручную:

```bash
docker compose run --rm app python -m newsbot.workflow --publish 123
```

Публикация требует `BOT_TOKEN` и `TELEGRAM_CHANNEL_ID` в серверном `.env`. Перед ней проверь предпросмотр. Автопубликация выключена.

## Заголовок, карточка и иллюстрация (новости)

При публикации новости в потоке `interesting_news` система собирает:

1. **Короткий заголовок-тизер** (2–5 слов) по схеме «субъект + глагол», завершённый многоточием, чтобы интриговать без раскрытия сути (например «Скалони высказался…»). Генерируется моделью `FILTER_MODEL` при сборке карточки и кэшируется на 3 дня (`events.headline_text`).
2. **Карточку** (`generate_news_card`): случайный фон + фото (или эмблема/флаг, разрешённые по заголовку) + тизер. Отделена от карточки результата матча.
3. **Иллюстрацию** по цепочке фолбэков: медиа из RSS → фото Wikimedia Commons по главному субъекту новости → AI-фото (`IMAGE_GEN_*`) → карточка только с фоном и заголовком → текст без картинки. Публикация не блокируется отсутствием фото или заголовка.

Черновик новости пишется моделью в тоне живого футбольного комментатора: первый абзац — ударная зацепка, ключевые имена выделяются `<b>…</b>`, цитаты — `<i>…</i>`, 1–3 эмодзи, абзацы разделяются пустой строкой. В Telegram пропускаются только теги `<b>`/`<i>` (остальной HTML экранируется), ссылки вырезаются, слухи явно помечаются.

## Медиа-библиотека

Файлы для импорта положи в `/root/newsbot/asset_import`; контейнер видит эту папку как `/imports`. Импортированные материалы копируются в постоянный Docker volume.

Показать библиотеку:

```bash
docker compose run --rm app python -m newsbot.assets --list
```

Добавить фон (лицензия или основание разрешения обязательны):

```bash
docker compose run --rm app python -m newsbot.assets --add /imports/stadium.png --type background --name "Стадион ночью" --license "Указать лицензию или разрешение" --credit "Автор"
```

Добавить эмблему:

```bash
docker compose run --rm app python -m newsbot.assets --add /imports/zenit.png --type crest --team-key zenit --name "Зенит" --aliases "Зенит;Зенит Санкт-Петербург" --license "Указать лицензию или разрешение"
```

Удалить ресурс библиотеки по его ID (удалится и файл):

```bash
docker compose run --rm app python -m newsbot.assets --remove 12
```

## Секреты, резервная копия и данные

Секреты хранятся в `/root/newsbot/.env`. Файл не коммитится. Никогда не отправляй его содержимое в чат и не запускай `cat .env` в общем логе.

Создать SQLite-резервную копию в отдельной папке на VPS:

```bash
mkdir -p /root/newsbot-backups
docker compose run --rm -v /root/newsbot-backups:/backups app python -c 'import sqlite3,datetime; s=sqlite3.connect("/data/newsbot.sqlite3"); d=sqlite3.connect("/backups/newsbot-"+datetime.datetime.now().strftime("%Y%m%d-%H%M%S")+".sqlite3"); s.backup(d); d.close(); s.close()'
ls -lh /root/newsbot-backups
```

Данные базы и импортированные эмблемы/фоны находятся в Docker volume. `docker compose down` останавливает проект и удаляет контейнер/сеть, но оставляет volume. **Не используй `docker compose down -v`**: флаг `-v` удалит постоянные данные бота.

Проверить том и свободное место:

```bash
docker volume ls | grep newsbot
df -h /
```

## Редакционная модель: два потока

Разделение на два потока уже реализовано в коде: завершённый матч распознаётся как `match_result` и идёт в поток `match_results`, остальные события — в `interesting_news` (колонка `events.publication_lane`).

1. **Результаты матчей** (`match_results`). Публикуются завершённые матчи с проверенным счётом из выбранных соревнований (`match_results.selected_competitions` в `editorial.yaml`); режим — `match_results.mode`. Требует двух независимых издателей. Используется отдельная карточка результата (счёт + эмблемы); модуль `generate_scorecard` заморожен — при работе над новостями его не менять.
2. **Интересные новости** (`interesting_news`). Трансферы, заявления, рекорды, важные события. ИИ оценивает интересность отдельно от надёжности источника (`interest_score`, порог `min_interest_score` = 7/10), пишет черновик в тоне комментатора и помогает подобрать заголовок и иллюстрацию. В ручной очереди достаточно одного доверенного издателя; дневной лимит публикаций — `interesting_news.daily_limit` (сейчас 2).

Оба потока сохраняют фактологическую проверку (счёт и цитаты только из RSS, без выдумок) и ручное одобрение.
