# Deploying the MT5 web app on a Windows VPS

The application and MetaTrader 5 must run on the same Windows VPS. Keep MT5's
server configuration and encryption key in the VPS `.env` file. Do not put
broker credentials, database passwords, SMTP credentials, or encryption keys
in the browser, source control, or chat.

## 1. Prepare the VPS

1. Install Python matching the project's virtual environment requirements.
2. Copy the project, the portable MT5 terminal, the compiled EA (`.ex5`), and
   required tester data to the VPS.
3. Install project dependencies in a dedicated virtual environment:

   ```powershell
   .\venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env`. Set the terminal path and EA name. For
   single-operator/legacy use, set a unique web username/password and keep
   `MT5_LEGACY_BASIC_AUTH_ENABLED=1`. Restrict access to `.env` using Windows
   file permissions.
5. Local development defaults to SQLite at
   `sqlite:///workspace/jobs.sqlite3`. For multi-user deployment, create a
   PostgreSQL database and set `DATABASE_URL` to its connection URL. The
   PostgreSQL driver is installed from `requirements.txt`. Keep the database
   private to the application host and back it up independently.
6. To prepare multi-user accounts, configure the SMTP host, port, sender and
   optional SMTP username/password. `MT5_SMTP_USE_SSL=1` uses implicit TLS
   (usually port 465); set it to `0` to require STARTTLS (usually port 587).
   Set `MT5_PUBLIC_BASE_URL` to the final HTTPS origin; generate a Fernet key
   on the server with
   `.\venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   and store it only in `MT5_CREDENTIAL_ENCRYPTION_KEY`. Protect and back up
   this key separately from PostgreSQL. Losing it makes stored MT5 passwords
   unrecoverable.
7. Keep `MT5_API_HOST=127.0.0.1`. The application must not be directly exposed
   on a public interface.

## 2. Add HTTPS reverse proxy

Install Caddy on the VPS and configure a DNS `A`/`AAAA` record for the domain
to point to the VPS. Copy `Caddyfile.example` to `Caddyfile`, replacing
`your-domain.example` with the real domain. Start Caddy as a Windows service
using the Caddy service installation procedure.

Allow inbound TCP ports 80 and 443 for Caddy and keep port 8000 blocked from
external networks. Caddy obtains and renews the HTTPS certificate. Only use
the application through `https://your-domain.example`.

## 3. Start the application

From the project directory, start the API with the virtual-environment Python:

```powershell
.\venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Open `https://your-domain.example/ui`. In legacy mode, authenticate with
`MT5_WEB_USERNAME` / `MT5_WEB_PASSWORD`. In account mode, users sign in with
their verified email and password. User sessions use an HttpOnly, SameSite
cookie; HTTPS is required.
For local-only preview, `MT5_ALLOW_LOCAL_NO_AUTH=1` skips Basic Auth only when
both the client and requested host are loopback (`localhost`, `127.0.0.1`, or
`::1`). Leave this unset on VPS deployments; requests through a public hostname
require an authenticated session.

Public registration is disabled by default. Enable it only after PostgreSQL,
HTTPS, SMTP and the encryption key are configured, then set
`MT5_PUBLIC_SIGNUP_ENABLED=1`, `MT5_EMAIL_VERIFICATION_ENABLED=1`, and
`MT5_ACCOUNT_RECOVERY_ENABLED=1`, `MT5_COOKIE_SECURE=1`, and
`MT5_LEGACY_BASIC_AUTH_ENABLED=0`. Set `MT5_ACCOUNT_RECOVERY_READY=1` only
after a recovery/support process is ready;
the shared Basic Auth path is intentionally incompatible with public
multi-user mode. Users can request a password-reset link; tokens expire after
one hour and a successful reset invalidates all existing sessions.
Registration uses email verification links
that expire after 24 hours; passwords are stored using scrypt, sessions as
hashed tokens, and MT5 passwords encrypted with the external Fernet key. The
key must remain available to the application/worker and must not be stored in
PostgreSQL.

Each user can save one MT5 account. Its password is encrypted at rest and is
decrypted only by the background worker when that user's connection check or
optimization starts. Job status, latest-job details, result downloads, and
reports are scoped to the owning user. Existing SQLite jobs are assigned to
the legacy account when the schema is upgraded. Switching `DATABASE_URL` to a
new PostgreSQL database does not automatically copy existing SQLite jobs.

The UI's MT5 status panel checks server-side configuration, the MetaTrader5
Python package, terminal executable, and configured EA. Use **Kiểm tra kết nối
broker** to verify the configured account through MT5. This check is queued
behind optimization jobs, briefly starts a terminal session, and closes it
afterward; close the portable terminal first. It does not place trades, and the
account password is never returned to the browser or stored in the job record.
After a successful check, the UI shows the MT5 terminal build. Recent job
failures are shown in the status panel. EA compilation is not part of this web
app: compile `.mq5` in MetaEditor and place the resulting `.ex5` under
`MQL5\\Experts`.

When the uploaded EA parameter file has no enabled optimization parameters,
the API rejects the request before queueing it and tells the user to upload the
matching optimization-range `.set` file or mark at least one numeric parameter
with `Y`. MQL5 Random Delay follows the platform's documented distribution:
90% of delays are 0-8 seconds and 10% are 9-18 seconds. It cannot be combined
with the "Open prices only" tick model; the UI and API both validate this.
See the [MQL5 Strategy Tester execution-delay documentation](https://www.metatrader5.com/en/terminal/help/algotrading/testing#trade_mode).

Optimization requests are persisted in the configured database and
processed one at a time by a background worker. The browser polls `/jobs/{id}`
for queued/running/completed/failed status, queue position, and trial progress.
The system status panel refreshes the total queue count every 15 seconds. A
running job is marked failed after an application restart; queued jobs are
resumed. Uploaded input files are removed when a job finishes.

To protect a single VPS, each job is capped at 100 trials, at most five jobs
may wait in the queue, and each client IP may submit at most three optimization
requests per minute. Requests exceeding a limit receive HTTP 429; queued jobs
and trial limits apply to all authenticated users. Behind Caddy, rate limiting
uses the client IP forwarded by the trusted local reverse proxy.

The current implementation supports per-account data ownership but still runs
one in-process worker against one portable MT5 terminal. Run exactly one API
process; multiple Uvicorn workers would each start a worker and could race for
the same MT5 terminal. Rate limits are also process-local, so do not scale out
API processes yet. Do not expose the service without HTTPS. MT5 symbol discovery
requires that the portable terminal is not already running; the app then
resolves the uploaded set file's `CurrencyList` against symbols supplied by
the configured broker.
