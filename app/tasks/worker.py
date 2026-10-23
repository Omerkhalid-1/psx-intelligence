"""Celery worker / beat entrypoint:

    celery -A app.tasks.worker:celery worker --loglevel INFO
    celery -A app.tasks.worker:celery beat --loglevel INFO
"""

from dotenv import load_dotenv

load_dotenv()  # no-op without a .env file; real environment variables take precedence

from app import create_app  # noqa: E402

flask_app = create_app(service_name="worker")
celery = flask_app.extensions["celery"]
