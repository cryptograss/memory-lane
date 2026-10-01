"""Preview settings: a branch's code against the record, without risking it.

For trying UI and logic changes without merging and redeploying: run the
dev server from a branch's checkout; its templates and code reload on
save. Two modes:

  PREVIEW_DB=live (default): the live record, every session read-only
    (`default_transaction_read_only`). Shows real Motions as they happen;
    unreviewed code can read the record but never change it.

  PREVIEW_DB=copy: a writable copy of the record in its own Postgres,
    refreshed on demand by dumping the live one. Sign in, post, mention:
    everything works, and nothing reaches the real record.

    POSTGRES_PASSWORD=... DJANGO_SETTINGS_MODULE=memory_viewer.settings_preview \\
        python manage.py runserver 0.0.0.0:4001

Container port 4001 is routed to https://justin1.hunter.cryptograss.live/.
"""
import os

from .settings import *  # noqa: F401,F403

DEBUG = False  # public URL: no debug pages
ALLOWED_HOSTS = ['*']

MODE = os.environ.get('PREVIEW_DB', 'live')

if MODE == 'copy':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ.get('PREVIEW_DB_NAME', 'magenta_memory'),
            'USER': os.environ.get('PREVIEW_DB_USER', 'magent'),
            'PASSWORD': os.environ.get('PREVIEW_DB_PASSWORD', 'staging'),
            'HOST': os.environ.get('PREVIEW_DB_HOST', 'magenta-staging-pg'),
            'PORT': os.environ.get('PREVIEW_DB_PORT', '5432'),
            'CONN_MAX_AGE': 0,
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': 'magenta_memory',
            'USER': 'magent',
            'PASSWORD': os.environ['POSTGRES_PASSWORD'],
            'HOST': os.environ.get('PREVIEW_LIVE_HOST', '10.0.0.2'),
            'PORT': '5432',
            'CONN_MAX_AGE': 0,
            'OPTIONS': {'options': '-c default_transaction_read_only=on'},
        }
    }

# Shown on the page, so nobody mistakes a preview for the real thing.
PREVIEW_LABEL = os.environ.get('PREVIEW_LABEL', 'preview')
# Live mode only: show the page as this person (composer included); sending
# fails, the record is read-only. In copy mode, sign in for real.
PREVIEW_VIEWER = os.environ.get('PREVIEW_VIEWER', '') if MODE != 'copy' else ''
