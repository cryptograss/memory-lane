"""Preview settings: a branch's code against the live record, read-only.

For trying UI and logic changes without merging and redeploying: run the
dev server from a branch's checkout and its templates and code reload on
save, showing the real Motions as they happen. Every database session is
read-only (`default_transaction_read_only`), so unreviewed code can read
the record but never change it; anything that writes fails loudly.

    POSTGRES_PASSWORD=... DJANGO_SETTINGS_MODULE=memory_viewer.settings_preview \\
        python manage.py runserver 0.0.0.0:4001

Container port 4001 is routed to https://justin1.hunter.cryptograss.live/.
"""
import os

from .settings import *  # noqa: F401,F403

DEBUG = False  # public URL: no debug pages
ALLOWED_HOSTS = ['*']

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('PREVIEW_DB_NAME', 'magenta_memory'),
        'USER': os.environ.get('PREVIEW_DB_USER', 'magent'),
        'PASSWORD': os.environ['POSTGRES_PASSWORD'],
        'HOST': os.environ.get('PREVIEW_DB_HOST', '10.0.0.2'),
        'PORT': os.environ.get('PREVIEW_DB_PORT', '5432'),
        'CONN_MAX_AGE': 0,
        'OPTIONS': {'options': '-c default_transaction_read_only=on'},
    }
}

# Shown on the page, so nobody mistakes a preview for the real thing.
PREVIEW_LABEL = os.environ.get('PREVIEW_LABEL', 'preview')
# Show the page as this person (composer included). Sending fails: read-only.
PREVIEW_VIEWER = os.environ.get('PREVIEW_VIEWER', '')
