# Moneyline

A private money tracker for personal income, spending, budgets, and transaction history. The Python/Flask server owns authentication and permissions; the browser uses HTML, CSS, and JavaScript. SQLite stores account and ledger rows. Every personal ledger query is scoped to the signed-in account. Administrators can approve accounts and review account totals and transaction details.

## Run on Windows

Open PowerShell in this folder:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:SECRET_KEY = (python -c "import secrets; print(secrets.token_hex(32))")
python init_admin.py
python app.py
```

Open `http://127.0.0.1:5000`. The first administrator username is `clayton paul otieno`; enter the desired password privately when `init_admin.py` prompts for it. Passwords are never stored in source code or in plaintext. Public registration only creates personal-user accounts; new accounts remain locked until an administrator approves them.

## Account approval and email

For local development, `MAIL_MODE=console` writes verification codes to the server log. Do not use console mode for a real deployment. To deliver codes by email, set `MAIL_MODE=smtp`, `MAIL_HOST`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, and `MAIL_FROM` in the process environment before starting the app. Approval emails contain a random code from 1 to 100,000. Codes are stored as password hashes, expire after 15 minutes, and are limited to five attempts. An administrator can resend a code from the admin page.

## Features

- Individual sign-in and server-enforced ownership of transaction and budget data.
- Administrator approval and email verification before ledger access.
- Money received/used entries with a purpose and automatic UTC timestamp.
- Balance totals, monthly budget progress, spending-by-purpose donut chart, and six-month income/spending chart.
- CSV export of a personal transaction history.
- Administrator view of account status, totals, and per-account transaction details.

## Free Live Pilot

The zero-cost setup keeps Flask templates, the API, and the PWA together on Render Free, stores data in Neon Free PostgreSQL, and sends verification mail through Google Apps Script. It needs no custom domain. It is a pilot setup, not a high-availability production service.

1. Push the project to a private GitHub repository on `main`. `.gitignore` excludes SQLite databases and `.env` files; never commit connection strings or secrets.
2. Create a free Neon project. Copy its pooled PostgreSQL connection string and keep it private.
3. Create a Google Apps Script project. Paste in `google_apps_script_mailer.gs`. In **Project Settings > Script Properties**, add `MAILER_SECRET` with a random value generated locally using `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Deploy as a web app that executes as you and allows access to **Anyone**. Keep the deployed `/exec` URL.
4. In Render, choose **New > Blueprint**, connect the GitHub repository, and apply `render.yaml`. Supply the Neon connection string for `DATABASE_URL`, the Apps Script `/exec` URL for `MAIL_WEB_APP_URL`, and the same `MAILER_SECRET` value. Render generates `SECRET_KEY`.
5. Render Free does not provide a service Shell. Create the live administrator from your computer by setting `DATABASE_URL` to the Neon connection string in a PowerShell session and running `python init_admin.py` once. The script initializes the schema and creates the admin in Neon. Remove `DATABASE_URL` from that shell when finished.
6. Open the Render HTTPS URL. Test registration with an inbox you can access, approve the account as admin, verify the email code, then install the PWA from the browser.

For a consumer Google account, Apps Script is limited to 100 email recipients per day. Render Free sleeps after 15 minutes without traffic and can take about a minute to wake. Neon Free currently includes 1 GB storage and 100 compute-unit hours per project monthly, and suspends idle compute after five minutes; its database data remains stored. Free-tier quotas and terms can change, and this setup has no production uptime guarantee or managed backups. Do not use it for sensitive financial records without keeping independent backups. For larger usage, upgrade to paid database/email services and load-test before inviting users.
