# Moneyline

A private money tracker for personal income, spending, budgets, and transaction history. The Python/Flask server owns authentication and permissions; the browser uses HTML, CSS, and JavaScript. SQLite stores account and ledger rows. Every personal ledger query is scoped to the signed-in account. Users can access the ledger immediately after creating an account. Administrators can view the registered-user count and block or unblock personal accounts.

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

Open `http://127.0.0.1:5000`. Run `python init_admin.py` to create or reset the administrator. The defaults are username `oclayton paul otieno` and email `claytonotieno5@gmail.com`; enter the password privately at the prompt. Passwords are hashed and never stored in source code or in plaintext. Users can register and sign in immediately without email verification. They can sign in using either their username or email.

## Features

- Individual sign-in and server-enforced ownership of transaction and budget data.
- Immediate ledger access after account creation; no approval or email verification.
- Money received/used entries with a purpose and automatic UTC timestamp.
- Separate dashboard, transactions, scan/import, voice/coach, and profile pages with shared navigation.
- Private profile photos during signup or from the signed-in dashboard.
- CSV statement imports and on-device photo OCR that prepare an editable preview; only explicitly approved rows are saved.
- Voice transaction entry with a transcript/details review before saving, plus a private, ledger-based budgeting coach.
- User-owned transaction deletion with a confirmation prompt.
- Balance totals, monthly budget progress, spending-by-purpose donut chart, and six-month income/spending chart.
- CSV export of a personal transaction history.
- Administrator-only registered-user count and account blocking controls.
- User leaderboard awards 10 points per recorded transaction and shows usernames only.

## Statement import, photos, and voice

The import tool accepts UTF-8 CSV files with `type` (or `direction`), `amount`, and `purpose` (or `description`) columns. Optional date columns are read when present. It recognizes explicit received/income/credit/deposit and used/expense/debit/withdrawal values; ambiguous or unreadable rows are skipped rather than guessed. Excel workbooks and PDFs are not supported.

Statement photos are processed in the browser with Tesseract.js and English OCR. The browser downloads the OCR library and language data from jsDelivr when a photo is scanned; the photo itself is not sent to Moneyline or that CDN. OCR can miss rows or misread amounts and labels, so compare with the source statement; every detected preview row is editable and must be explicitly approved before it is added. Profile photos are limited to 2 MB and stored with the account.

Voice entry uses the browser's speech-recognition feature where available. The browser speech service is the default and requires the user to confirm that audio may be sent to its provider; on-device recognition is available only in compatible browsers with language support installed. Current Chrome or Edge is recommended. Microphone access requires permission and a secure context (localhost or HTTPS). Browser and device audio cleanup varies, and noise filtering cannot be guaranteed. Only clear statements with a received/used amount and purpose are recognized; the user reviews the editable preview and explicitly approves it before saving. Browsers without speech recognition cannot transcribe audio in this app.

The statement scanner can open a device camera (rear-facing camera preferred) on localhost/HTTPS after permission is granted, capture an image, and run the same on-device OCR preview as a selected photo. Camera access can also be unavailable due to browser, device, or permission restrictions; selecting an existing image remains available.

The Moneyline coach answers questions about the signed-in user's recorded totals and gives basic budgeting prompts. It is not an AI service and does not provide investment, tax, credit, or debt recommendations. It rejects non-ledger questions and does not send ledger data to an external service.

## Free Live Pilot

The zero-cost setup keeps Flask templates, the API, and the PWA together on Render Free and stores data in Neon Free PostgreSQL. It needs no custom domain. It is a pilot setup, not a high-availability production service.

1. Push the project to a private GitHub repository on `main`. `.gitignore` excludes SQLite databases and `.env` files; never commit connection strings or secrets.
2. Create a free Neon project. Copy its pooled PostgreSQL connection string and keep it private.
3. In Render, choose **New > Blueprint**, connect the GitHub repository, and apply `render.yaml`. Supply the Neon connection string for `DATABASE_URL`. Render generates `SECRET_KEY`.
4. Render Free does not provide a service Shell. Create or reset the live administrator from your computer by setting `DATABASE_URL` to the Neon connection string in a PowerShell session and running `python init_admin.py`. Use the same deployed code and enter the admin password only at the hidden prompt. The script updates the account matching the supplied username or email, or creates it if neither exists. Remove `DATABASE_URL` from that shell when finished.
5. Open the Render HTTPS URL and create a user account, then install the PWA from the browser.

For a consumer Google account, Apps Script is limited to 100 email recipients per day. Render Free sleeps after 15 minutes without traffic and can take about a minute to wake. Neon Free currently includes 1 GB storage and 100 compute-unit hours per project monthly, and suspends idle compute after five minutes; its database data remains stored. Free-tier quotas and terms can change, and this setup has no production uptime guarantee or managed backups. Do not use it for sensitive financial records without keeping independent backups. For larger usage, upgrade to paid database/email services and load-test before inviting users.
