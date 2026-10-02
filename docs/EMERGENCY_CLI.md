# Standalone PAPER kill (Windows, no Docker/web server required)

Use the same backend environment and database as the worker. Database migrations
must already be applied. The database must be reachable; the CLI does not start
an API server, create a replacement database, or bypass owner authentication.

From PowerShell in `D:\self-ai\ai-trading-system\backend`:

```powershell
.\.venv\Scripts\python.exe -m app.emergency.cli kill --reason "Owner requests emergency entry inhibition" --confirm "KILL PAPER"
```

Enter the configured dashboard owner's password at the hidden terminal prompt.
If terminal echo cannot be disabled, the command refuses rather than echoing it.
An optional `--username` selects the login name; it does not grant another role.
Do not put passwords in command arguments, shell history or source files.
Explicit `--password-stdin` supports controlled automation via one password line;
never print that input or embed a real password in a command example/script.

Successful output says `ENTRY_INHIBITION_ONLY` and exit status is zero. This means
the shared audited kill state is persisted, **not** that positions were closed.
The worker observes the persisted state on its next cycle, and execution
preflight checks it independently. Existing safe exits remain allowed. Restart
does not clear the kill. There is deliberately no CLI re-arm command: owner
review and the existing authenticated health/reconciliation checks still apply.

This command supports PAPER only. It shares login lockout policy with the
dashboard, revokes its temporary login session after the request, never prints
tokens, and records authenticated refusal reasons without confirmation/password
contents. Authentication/configuration/database failures return nonzero and do
not claim success. If a failure occurs after mutation (for example while closing
the login session), inspect persisted state: failure does not imply the kill was
undone. A database outage cannot be worked around by claiming a durable kill.

Verification uses an actual separate process and a shared isolated SQLite
database, without an HTTP server, while a PAPER worker is running and after its
restart. Live PostgreSQL deployment, non-PAPER controls and Groww execution remain
unverified. This is not an operating-system process kill or a broker flatten.
