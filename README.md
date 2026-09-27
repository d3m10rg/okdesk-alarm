# Okdesk Alarm

Небольшой клиент для Windows, который напрямую обращается к
[`sd.cpsupport.ru`](https://sd.cpsupport.ru/) под учетной записью пользователя и
циклически проигрывает аудиофайл:

- при появлении новой заявки;
- при появлении нового комментария в заявке, добавленной пользователем для
  отслеживания (параметр `tracked_issue_ids`).

Программа работает на Python 3 без сторонних библиотек.

## Настройка

1. Установите Python 3 для Windows и этот репозиторий.
2. Скопируйте `alarm_client.example.json` в `alarm_client.json`.
3. Заполните `login` и `password` данными своей учетной записи Okdesk.
4. Укажите номера заявок в массиве `tracked_issue_ids`. Если комментарии
   отслеживать не нужно, оставьте пустой массив `[]`.
5. Запустите программу из ее папки:

```cmd
cd C:/<путь_до_папки>
python ticket_alarm_client.py
```

Пример конфига:

```json
{
  "okdesk_url": "https://sd.cpsupport.ru",
  "login": "user@example.com",
  "password": "your_password",
  "tracked_issue_ids": [16540, 16541],
  "audio_file": "sample-6s.wav",
  "poll_interval_seconds": 60,
  "state_file": "alarm_state.json",
  "debug": false,
  "request_timeout_seconds": 15
}
```

## Поведение

Программа в обычном режиме начинает проигрывать аудиофайл только при появлении новых заявок. Если необходимо отслеживать появление новых комментариев в определенной заявке, тогда в config.json в параметре tracked_issue_ids укажите в квадратных скобках номер заявки (если таких заявок несколько, то указывайте номер через запятую и пробел: [16443, 16444, 17041])
Чтобы остановить сигнал, введите `awake`.

Команды во время работы:

- `awake` — остановить сигнал;
- `test sound` — проверить звук;
- `last` — показать последнюю заявку и состояние комментариев.

Для диагностики установите `"debug": true`.

## API

Используются официальные методы Okdesk:

- `POST /api/v1/users/sign_in` — получение API-ключа по логину и паролю;
- `GET /api/v1/issues/list` — получение последней заявки;
- `GET /api/v1/issues/{issue_id}/comments` — комментарии отслеживаемой заявки.

[Документация REST API Okdesk](https://okdesk.ru/sql_apidoc/)

## Проверка

```cmd
python -m unittest discover -s tests -v
```
