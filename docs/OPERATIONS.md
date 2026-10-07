# Памятка: NewsBot на VPS

Команды ниже выполняются в SSH-сессии на VPS. В примерах репозиторий находится в `/opt/newsbot`, а ID события заменяется числом из вывода очереди.

## Быстрый старт и обновление

```bash
cd /opt/newsbot
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

Показать события по состоянию и редакционной очереди:

```bash
docker compose run --rm app python -m newsbot.workflow --state awaiting_review
docker compose run --rm app python -m newsbot.workflow --state awaiting_review --lane match_results
docker compose run --rm app python -m newsbot.workflow --state awaiting_review --lane interesting_news
docker compose run --rm app python -m newsbot.workflow --state awaiting_verification
docker compose run --rm app python -m newsbot.workflow --state approved
docker compose run --rm app python -m newsbot.workflow --state retry_wait
docker compose run --rm app python -m newsbot.workflow --state published
docker compose run --rm app python -m newsbot.workflow --state skipped
docker compose run --rm app python -m newsbot.workflow --state failed
```

- `awaiting_review`: ИИ подтвердил футбольную релевантность и редакционный порог потока; нужна редакторская проверка. Для интересных новостей достаточно одного доверенного издателя, для результатов матча — двух.
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
docker compose run --rm app python -m newsbot.workflow --state awaiting_review --lane match_results
docker compose run --rm app python -m newsbot.workflow --state awaiting_review --lane interesting_news
```

Подставь ID события вместо `123`. Обе очереди требуют завершённой AI-оценки и футбольной релевантности. Для интересной новости нужен один доверенный издатель и оценка не ниже 7/10; для результата матча — два независимых издателя, проверенные команды, счёт, турнир и статус. Перед публикацией оба потока требуют ручного одобрения. Приоритет — отдельная метка:

```bash
docker compose run --rm app python -m newsbot.workflow --approve 123 --priority routine
docker compose run --rm app python -m newsbot.workflow --approve 123 --priority major_match
```

Для проверенного заявления организации от её собственного официального источника можно использовать исключение по подтверждениям:

```bash
docker compose run --rm app python -m newsbot.workflow --approve 123 --official-statement
```

Это не отменяет AI-проверку, футбольную релевантность и критерии выбранного редакционного потока.

Пропустить событие можно только с причиной:

```bash
docker compose run --rm app python -m newsbot.workflow --skip 123 --reason "Причина пропуска"
```

Предпросмотр без публикации и без отправки сообщения в канал:

```bash
docker compose run --rm app python -m newsbot.workflow --preview 123
```

Для обычной новости выбрать разрешённое к повторному использованию фото (событие должно быть одобрено; нужен Groq Vision):

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

## Медиа-библиотека

Фоны и другие временные файлы для импорта положи в `/opt/newsbot/asset_import`; контейнер видит эту папку как `/imports`.

Папка `assets/logos/background` в репозитории — исходная коллекция фонов, доступная контейнеру как `/logos/background`. После получения новых файлов через Git нужно один раз зарегистрировать их в постоянном Docker volume. Команда безопасна для повторного запуска: уже импортированные по SHA-256 файлы будут посчитаны как дубликаты. В `--license` укажи фактическое основание использования своих изображений:

```bash
docker compose run --rm app python -m newsbot.assets --import-backgrounds /logos/background --license "Основание использования изображений"
```

Эмблемы команд складывай в `/opt/newsbot/assets/logos` внутри репозитория. Папка уже добавлена в Git и подключена в контейнер как `/logos` только для чтения. После `git pull` новые файлы сразу будут доступны контейнеру. Чтобы карточки матчей находили эмблему по названию команды, зарегистрируй её в постоянной медиабиблиотеке:

```bash
docker compose run --rm app python -m newsbot.assets --add /logos/zenit.png --type crest --team-key zenit --name "Зенит" --aliases "Зенит;Зенит Санкт-Петербург" --license "Основание использования"
```

Ключ и варианты названия должны соответствовать команде в источниках. Импортированные ресурсы копируются в постоянный Docker volume; повторно загружать их после пересборки контейнера не требуется.

Показать библиотеку:

```bash
docker compose run --rm app python -m newsbot.assets --list
```

Добавить фон (лицензия или основание разрешения обязательны):

```bash
docker compose run --rm app python -m newsbot.assets --add /imports/stadium.png --type background --name "Стадион ночью" --license "Указать лицензию или разрешение" --credit "Автор"
```

Добавить эмблему из `/logos` в медиабиблиотеку:

```bash
docker compose run --rm app python -m newsbot.assets --add /logos/zenit.png --type crest --team-key zenit --name "Зенит" --aliases "Зенит;Зенит Санкт-Петербург" --license "Основание использования"
```

Удалить ресурс библиотеки по его ID (удалится и файл):

```bash
docker compose run --rm app python -m newsbot.assets --remove 12
```

## Секреты, резервная копия и данные

Секреты хранятся в `/opt/newsbot/.env`. Файл не коммитится. Никогда не отправляй его содержимое в чат и не запускай `cat .env` в общем логе.

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

Потоки разделены в базе (`publication_lane`) и в списке очереди. Оба требуют AI-подтверждения футбольной релевантности, русского черновика и ручного одобрения. Для интересной новости достаточно одного доверенного издателя; для результата матча нужны два независимых подтверждения. Автопубликация в Docker Compose остаётся выключенной.

### Интересные новости

ИИ ставит отдельную оценку интересности читателю от 0 до 10. Надёжность источника хранится отдельно. По умолчанию в редакционную очередь проходят новости с интересностью не ниже 7/10; очередь сортируется по оценке. За московский календарный день можно одобрить и опубликовать не более двух таких новостей. Ограничение проверяется при одобрении и ещё раз прямо перед отправкой.

```bash
docker compose run --rm app python -m newsbot.workflow --state awaiting_review --lane interesting_news
```

### Результаты матчей

ИИ отправляет завершённые матчи в отдельную очередь только если распознаны команды, точный счёт, турнир и статус `final`/`extra_time`. Порог 10/10 и дневной лимит интересных новостей к результатам не применяются. Результаты всё равно одобряются вручную.

```bash
docker compose run --rm app python -m newsbot.workflow --state awaiting_review --lane match_results
```

Список разрешённых лиг и режим задаются в `/opt/newsbot/editorial.yaml` и в корне локального репозитория. Сравнение использует точное название или alias без учёта регистра; если лента называет турнир иначе, добавь её написание в `aliases`:

- `mode: selected` (по умолчанию): в очередь попадают только результаты соревнований из `selected_competitions`; список включает крупные турниры и лиги, а каждое событие всё равно требует ручного одобрения.
- `mode: tournament`: к списку выбранных лиг добавляются все подтверждённые матчи активного турнира. Остальные соревнования остаются на проверке.

Чтобы во время чемпионата мира включить режим турнира, укажи в YAML, например:

```yaml
match_results:
  mode: tournament
  active_tournament: "Чемпионат мира"
  tournament_aliases:
    - FIFA World Cup
    - Чемпионат мира по футболу
```

После изменения `editorial.yaml` на VPS пересобери контейнер:

```bash
docker compose up -d --build app
```

Выбери событие по числовому ID из очереди. Для результата проверь карточку через `--preview ID`, одобри через `--approve ID`, затем отправь через `--publish ID`. Для интересной новости после одобрения выбери иллюстрацию через `--select-image ID` и проверь `--preview ID`.

### AI-анализ и HTTP 413

Одна команда `--analyze` обрабатывает до 15 событий небольшими группами. При ответе Groq `HTTP 413` запрос автоматически повторяется с меньшим числом событий; если одно событие тоже не помещается, для него назначается повторная попытка, а остальные группы продолжают обрабатываться.
