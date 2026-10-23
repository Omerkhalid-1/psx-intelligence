"""WSGI entrypoint: `gunicorn wsgi:app` or `flask --app wsgi run`."""

from dotenv import load_dotenv

load_dotenv()  # no-op without a .env file; real environment variables take precedence

from app import create_app  # noqa: E402

app = create_app()
