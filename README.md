# End of Licenses Dashboard

Внутренний сервис для ручного учета лицензий и контроля сроков окончания.

## Что умеет v1

- Dashboard на порту `8000`.
- Верхние KPI: всего, используется, свободно, истекает за 30 дней.
- Таблица лицензий со статусами.
- Ручное добавление записей.
- Ручное удаление записей.
- JSON API для интеграций.
- Healthcheck.
- Хранение данных в `data/licenses.json`.

## Структура

```text
End-of-licenses/
├── app.py
├── requirements.txt
├── README.md
├── data/
│   └── licenses.json
├── templates/
│   ├── layout.html
│   ├── dashboard.html
│   └── license_form.html
└── static/
    └── style.css
```

## Установка на RED OS / Linux без Docker

```bash
sudo dnf install -y python3 python3-pip
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Запуск

```bash
python3 app.py
```

Открыть:

```text
http://SERVER_IP:8000
```

## API

```text
GET /api/licenses
GET /health
```

Пример проверки:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/licenses
```

## Следующие этапы

- Редактирование записей.
- Excel import.
- Nginx Basic Auth.
- Интеграция с Vinteo API.
- Endpoint под Zabbix.
- Systemd unit.
