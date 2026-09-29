"""Staging settings: a dev server against a throwaway copy of the database.

For iterating on views quickly with real data and no risk to the record.
The intended setup, on hunter:

    docker run -d --name magenta-staging-pg --network magenta-net \
        -e POSTGRES_USER=magent -e POSTGRES_PASSWORD=staging \
        -e POSTGRES_DB=magenta_memory postgres:16-alpine
    pg_restore -h magenta-staging-pg -U magent -d magenta_memory \
        --no-owner --no-privileges -j 4 <backup.dump>
    DJANGO_SETTINGS_MODULE=memory_viewer.settings_staging \
        python manage.py runserver 0.0.0.0:4001

Container port 4001 is mapped to justin1.hunter.cryptograss.live by the
per-user Caddy config, so the result is viewable by anyone on the team.

Everything is overridable by environment for other hosts and ports.
"""
import os

from .settings import *  # noqa: F401,F403

DEBUG = True

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('STAGING_DB_NAME', 'magenta_memory'),
        'USER': os.environ.get('STAGING_DB_USER', 'magent'),
        'PASSWORD': os.environ.get('STAGING_DB_PASSWORD', 'staging'),
        'HOST': os.environ.get('STAGING_DB_HOST', 'magenta-staging-pg'),
        'PORT': os.environ.get('STAGING_DB_PORT', '5432'),
        'CONN_MAX_AGE': 0,
    }
}

ALLOWED_HOSTS = ['*']
