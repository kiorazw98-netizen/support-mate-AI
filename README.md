# Support Mate

A lightweight customer-support chatbot and admin dashboard built with FastAPI, SQLite, HTML, CSS, and JavaScript.

## Features

- Answer customer questions from your editable business knowledge base.
- Optionally use an OpenAI-compatible chat-completions API to phrase answers using knowledge-base information.
- Create persistent support tickets and flag unanswered questions for human follow-up.
- Review conversations, track ticket statuses, reply to customers, and resolve tickets in the dashboard.
- Store conversations, tickets, and articles locally in SQLite.

The knowledge base is prefilled with sample business hours, return, and shipping answers. Replace these examples with your own business information before sharing the app with customers.

## Run locally

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Optional AI API

Without an API key the chatbot answers matching questions directly from the knowledge base and still escalates unanswered questions. To enable AI-generated answers, set `AI_API_KEY` (or `OPENAI_API_KEY`), and optionally `AI_API_BASE_URL` and `AI_MODEL`, before starting the server. The configured API must support the OpenAI-compatible `/chat/completions` endpoint.

Use `.env.example` as a reference; the application does not load a `.env` file automatically. Environment variables are never stored in the database.

## Admin login

The customer chat remains public. Dashboard, ticket management, and knowledge-base administration require the single admin account configured with `ADMIN_USERNAME` and `ADMIN_PASSWORD`. The password must be at least 12 characters. Set `SESSION_SECRET` to a separate random secret of at least 32 characters; for example, generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

Set these environment variables before starting the server. Admin sessions expire after 12 hours, use HTTP-only, SameSite cookies, and require CSRF tokens for changes. For HTTPS deployments, set `APP_ENV=production` or `SESSION_COOKIE_SECURE=true`. Admin endpoints return an error when credentials or the signing secret are missing or too short; they never fall back to unauthenticated access.

Customer conversations use a separate random access token, so knowing a conversation ID alone does not grant access to its message history.

For PowerShell, set the values before launching Uvicorn:

```powershell
$env:ADMIN_USERNAME = "your-admin-name"
$env:ADMIN_PASSWORD = Read-Host "Choose an admin password"
$env:SESSION_SECRET = (python -c "import secrets; print(secrets.token_urlsafe(32))")
uvicorn main:app --reload
```

Add login rate limiting at the reverse proxy or hosting layer before public deployment.

## Data and API

The database defaults to `support_mate.db` beside `main.py`. Set `DATABASE_PATH` to choose another location.

Interactive API documentation is available at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs). Before public deployment, also configure HTTPS, login rate limiting, backups, and other deployment-specific protections.
