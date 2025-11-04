# AmoCRM ↔ Google Sheets Integration (FastAPI, Python)

## 🚀 Возможности

- Импорт клиентов из Google Sheets → AmoCRM (контакты и сделки)
- Обратная синхронизация сделок AmoCRM → таблицу
- Автоматическое обновление каждые 2–5 минут
- Поиск дубликатов по email/телефону
- Обработка rate limit (Google Sheets, AmoCRM)
- Пакетные обновления для экономии квоты Google

## ⚙️ Стек

- Python 3.10+
- FastAPI
- httpx
- google-auth-oauthlib / google-api-python-client
- APScheduler
- python-dotenv

## Взаимодействие

```bash
http://localhost:8000/google/oauth2/start - авторизация для Google Sheets
http://localhost:8000/google/sheets/read - чтение Google Sheets
http://localhost:8000/sync/once - синхронизация amoCRM и Google Sheets
http://localhost:8000/sync/pull_amocrm - синхронизация Google Sheets и amocrm

```

Предусмотрена автоматическая синхронизация: чтение из Google Sheets и записать в
amoCRM каждые 2 минуты, в обратную сторону каждые 5 минут.
Информация о синхронизации выводится в терминал, так же присутсвуют логи.

## 🔧 Установка

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

```

## Запуск

```bash
uvicorn main:app --reload
```
