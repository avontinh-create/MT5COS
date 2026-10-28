# Deploying the MT5 web app on a Windows VPS

The application and MetaTrader 5 must run on the same Windows VPS. Keep MT5's
account credentials in the VPS `.env` file and do not put broker credentials
in the browser or a source-control commit.

## 1. Prepare the VPS

1. Install Python matching the project's virtual environment requirements.
2. Copy the project, the portable MT5 terminal, the compiled EA (`.ex5`), and
   required tester data to the VPS.
3. Install project dependencies in a dedicated virtual environment:

   ```powershell
   .\venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env`. Set the broker login, password, server,
   terminal path, EA name, and unique web username/password. The web password
   must be at least 16 characters; sample placeholder credentials are rejected.
   Restrict access to `.env` using Windows file permissions.
5. Keep `MT5_API_HOST=127.0.0.1`. The application must not be directly exposed
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

Open `https://your-domain.example/ui` and authenticate with
`MT5_WEB_USERNAME` / `MT5_WEB_PASSWORD`. Basic authentication is enforced on
the UI, API, downloads, and API documentation. HTTPS is required because
browser Basic authentication sends credentials with each request.

Optimization requests are persisted as jobs in `workspace/jobs.sqlite3` and
processed one at a time by a background worker. The browser polls `/jobs/{id}`
for queued/running/completed/failed status and trial progress. A running job
is marked failed after an application restart; queued jobs are resumed. Uploaded
input files are removed when a job finishes.

The current implementation is intended for a single operator and a single
portable MT5 terminal. Run exactly one API worker; multiple Uvicorn workers
would each start a worker and could race for the same MT5 terminal. Do not
expose the service without HTTPS. MT5 symbol discovery requires that the
portable terminal is not already running; the app then resolves the uploaded
set file's `CurrencyList` against symbols supplied by the configured broker.
