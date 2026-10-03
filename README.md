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

## Deploy on Render

1. Push this project to a private GitHub repository. Keep `.env` files and SQLite database files out of Git.
2. In Render, choose **New > Blueprint**, connect the repository, and apply the settings from `render.yaml`.
3. Enter SMTP provider values when Render asks for `MAIL_HOST`, `MAIL_USERNAME`, `MAIL_PASSWORD`, and `MAIL_FROM`. Verification codes will not be delivered until these are configured correctly.
4. After the service deploys, open its Shell and run `python init_admin.py` once. The blueprint sets `DATABASE_PATH` to the persistent disk, so the account will be created in the live database. Use a unique email and a password with at least 10 characters.
5. Open the HTTPS URL Render assigns to the service.

The blueprint uses a paid web-service plan because it attaches a persistent disk for the SQLite database. Do not remove that disk or deploy multiple app instances with this SQLite setup. Keep backups private; SQLite database files are not encrypted at rest. For larger deployments, migrate to a managed PostgreSQL database and add login throttling, password reset, monitoring, and a tested backup/restore process. Never expose Flask's development server directly to the public internet.
