#!/usr/bin/env bash
# Startup gate — DEPLOY-004.
#
# Migrations run before the application starts, and the process refuses to serve
# if they fail. A trading system that comes up against a half-migrated schema
# will read and write the wrong shapes, so failing loudly is the only safe option.
set -euo pipefail

log() { printf '%s [entrypoint] %s\n' "$(date -Is)" "$*" >&2; }

: "${TRADING_MODE:=PAPER}"
log "TRADING_MODE=${TRADING_MODE}"

wait_for_db() {
    local attempts=${DB_WAIT_ATTEMPTS:-30}
    local i=1
    while [ "$i" -le "$attempts" ]; do
        if python -c "
import asyncio, sys
from app.db import session
async def main():
    try:
        await session.ping()
    except Exception as exc:
        print(exc, file=sys.stderr); sys.exit(1)
asyncio.run(main())
" 2>/dev/null; then
            log "database reachable"
            return 0
        fi
        log "waiting for database (${i}/${attempts})"
        sleep 2
        i=$((i + 1))
    done
    log "FATAL: database did not become reachable"
    return 1
}

case "${1:-serve}" in
    serve)
        wait_for_db
        log "applying migrations"
        alembic upgrade head
        log "migrations applied; starting API"
        exec uvicorn app.main:app \
            --host "${API_HOST:-0.0.0.0}" \
            --port "${API_PORT:-8000}" \
            --log-config /dev/null
        ;;
    migrate)
        wait_for_db
        exec alembic upgrade head
        ;;
    shell)
        exec /bin/bash
        ;;
    *)
        exec "$@"
        ;;
esac
