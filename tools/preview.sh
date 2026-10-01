#!/bin/bash
# Run a preview of this checkout: the dev server, reloading on every save.
#
#   tools/preview.sh            # live record, read-only
#   tools/preview.sh copy       # writable copy (refresh: tools/preview_refresh.sh)
#
# On hunter, container port 4001 is https://<user>1.hunter.cryptograss.live/.
# Copy mode accepts sign-ins exactly as production does -- your SSH key, via
#   ./magenta.sh login --base https://justin1.hunter.cryptograss.live
# -- against the same people (hunter's inventory), into the copy only.
set -euo pipefail
cd "$(dirname "$0")/.."

MODE=${1:-live}
PORT=${PREVIEW_PORT:-4001}
STATE=${PREVIEW_STATE:-$HOME/.local/state/magenta/preview}
mkdir -p "$STATE"

export DJANGO_SETTINGS_MODULE=memory_viewer.settings_preview
export PYTHONPATH=.
export PREVIEW_DB=$MODE
# Per start; devices are stored as hashes, so a new key signs nobody out.
export DJANGO_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')

if [ "$MODE" = copy ]; then
    # Who may sign in: the same people and keys production allows, from
    # hunter's inventory (public keys only).
    gh api repos/cryptograss/maybelle-config/contents/hunter/ansible/inventory.yml?ref=production \
        --jq .content | base64 -d | python3 -c '
import sys, yaml
for user in yaml.safe_load(sys.stdin)["all"]["vars"]["users"]:
    key = (user.get("ssh_pubkey") or "").strip()
    if key.startswith("ssh-"):
        print(user["name"] + " namespaces=\"magenta-motions\" " + key)
' > "$STATE/allowed_signers"
    export MOTION_ALLOWED_SIGNERS=$STATE/allowed_signers
    AS_OF=$(PGPASSWORD=${PREVIEW_DB_PASSWORD:-staging} psql -h "${PREVIEW_DB_HOST:-magenta-staging-pg}" -U magent \
        -d magenta_memory -Atc "select to_char(max(created_at) at time zone 'utc', 'Mon DD HH24:MI') from conversations_message" 2>/dev/null || echo '?')
    export PREVIEW_LABEL="preview · $(git branch --show-current) · writable copy as of $AS_OF UTC"
else
    export PREVIEW_VIEWER=${PREVIEW_VIEWER:-}
    export PREVIEW_LABEL="preview · $(git branch --show-current) · live, read-only"
fi

exec python3 manage.py runserver "0.0.0.0:$PORT"
