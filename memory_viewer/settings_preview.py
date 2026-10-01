"""Preview settings: a branch's code against the live record, without risking it.

For trying UI and logic changes without merging and redeploying: run the
dev server from a branch's checkout (tools/preview.sh); its templates and
code reload on save. Two modes:

  PREVIEW_DB=append (the default once the role exists): the live record,
    through the `memory_lane_preview` Postgres role, which can read
    everything and add, but never change or remove what is there. Sign in,
    post, mention: it all lands in the real record -- a preview's
    conversation is remembered like any other -- and no bug in unreviewed
    code can damage what's already written. Its only UPDATEs are the
    columns sign-in needs: a login code's used_at, a device's last_used_at
    and revoked_at. A branch with new migrations can't run here.

  PREVIEW_DB=live: the same, read-only (`default_transaction_read_only`):
    nothing a preview does is written. Uses the preview role when its
    password is present, else the application's own -- read-only by
    session default only, so prefer the role.

Container port 4001 is routed to https://justin1.hunter.cryptograss.live/.
"""
import os

from .settings import *  # noqa: F401,F403

DEBUG = False  # public URL: no debug pages
ALLOWED_HOSTS = ['*']

MODE = os.environ.get('PREVIEW_DB', 'append')
ROLE_PASSWORD = os.environ.get('MEMORY_LANE_PREVIEW_DB_PASSWORD', '')

if MODE == 'append' and not ROLE_PASSWORD:
    raise RuntimeError('PREVIEW_DB=append needs MEMORY_LANE_PREVIEW_DB_PASSWORD (the memory_lane_preview '
                       "role's, from the vault via the hunter deploy); or use PREVIEW_DB=live.")

if ROLE_PASSWORD:
    _user, _password = 'memory_lane_preview', ROLE_PASSWORD
else:
    _user, _password = 'magent', os.environ['POSTGRES_PASSWORD']

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'magenta_memory',
        'USER': _user,
        'PASSWORD': _password,
        'HOST': os.environ.get('PREVIEW_LIVE_HOST', '10.0.0.2'),
        'PORT': '5432',
        'CONN_MAX_AGE': 0,
    }
}
if MODE != 'append':
    DATABASES['default']['OPTIONS'] = {'options': '-c default_transaction_read_only=on'}

# Shown on the page, so nobody mistakes a preview for the real thing.
PREVIEW_LABEL = os.environ.get('PREVIEW_LABEL', 'preview')
# Devices enrolled here are real devices; their label says where they came from.
DEVICE_LABEL_PREFIX = 'preview · '
# Live mode only: show the page as this person (composer included); sending
# fails, the record is read-only. In append mode, sign in for real.
PREVIEW_VIEWER = os.environ.get('PREVIEW_VIEWER', '') if MODE == 'live' else ''
