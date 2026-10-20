# Hospital Queue Management System

A centralized hospital queue management platform built with Django, Django Channels, and PostgreSQL.

## Features
* **Patient Registration**: Kiosk and Reception workflows.
* **Concurrency-Safe**: Database row-locking prevents duplicate queue tickets.
* **Staff Dashboards**: Role-based access, patient status tracking, priority escalation, and cross-department routing.
* **Live Display**: Real-time WebSockets powered by Django Channels and Redis to show waiting tickets and announce them using Browser Text-to-Speech (TTS).
* **Reporting**: Basic daily metrics.

## Local Development
1. Create a virtual environment: `python -m venv venv`
2. Activate it: `.\venv\Scripts\activate` (Windows) or `source venv/bin/activate` (Mac/Linux)
3. Install dependencies: `pip install -r requirements.txt` (Make sure to include channels, daphne, psycopg2-binary, django-environ).
4. Run migrations: `python manage.py migrate`
5. Create a superuser: `python manage.py createsuperuser`
6. Run the ASGI server: `daphne hms_project.asgi:application` (or `python manage.py runserver` for basic dev).

Navigate to `http://127.0.0.1:8000/`.

## Production Deployment (Docker)
1. Install Docker and Docker Compose.
2. Run `docker-compose up -d --build`.
3. The app will be available on `http://localhost:8000`. Redis and Postgres will be automatically configured.

## Usage Guide
* Setup: Login to `/admin/` and create at least one `Department`, `Room`, and `Service`.
* Kiosk: `/` (Creates tickets).
* Dashboard: `/dashboard/` (Staff view to call tickets).
* TV Display: `/display/` (Open on a TV to see tickets being called via websockets).
