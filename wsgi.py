"""Gunicorn entrypoint: gunicorn -c gunicorn.conf.py wsgi:app"""

from app import app  # noqa: F401
