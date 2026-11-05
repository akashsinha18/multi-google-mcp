# multi-google-mcp

A remote MCP server (streamable HTTP) that gives Claude access to **multiple Google
accounts at once** — Gmail and Google Drive — for use as a **custom connector in
claude.ai** (web, desktop and mobile).

- One Google Cloud OAuth client shared by all accounts.
- Each connected account has an alias you choose (`personal`, `work`, …) plus its verified email.
- Search tools fan out to every account in parallel (`account="all"`) and tag each result with its account.
- Refresh tokens are Fernet-encrypted at rest in SQLite.
- The MCP endpoint is protected by Google sign-in, limited to `ALLOWED_EMAILS`.

```
claude.ai ──OAuth (Google sign-in, ALLOWED_EMAILS)──▶ /mcp  ──▶ tools
                                                         │
you (browser) ──Google sign-in──▶ /accounts/connect ──▶ Google consent for data account
                                                         └─▶ encrypted refresh token in SQLite
```

There are **two separate Google sign-ins**:

1. **Identity** (MCP login and `/accounts` admin login): only `openid` + `email`. It proves you're
   in `ALLOWED_EMAILS`. It grants no access to mail or files.
2. **Data accounts** (`/accounts/connect`): the Gmail/Drive scopes, one consent per account. These
   can be any Google accounts, e.g. a Workspace work account, even if it isn't in `ALLOWED_EMAILS`.

## Tools

| Tool | `account` | Annotations |
|---|---|---|
| `list_accounts()` | – | read-only |
| `gmail_search(query, account="all", max_results=20, page_token=None)` | alias / email / `all` | read-only |
| `gmail_get_thread(thread_id, account, max_body_chars=8000)` | alias / email | read-only |
| `gmail_list_labels(account)` | alias / email | read-only |
| `gmail_modify_labels(message_or_thread_id, account, add=[], remove=[], target="auto")` | alias / email | write, non-destructive, idempotent |
| `gmail_create_draft(account, to, subject, body, cc=None, reply_to_thread_id=None)` | alias / email | write, non-destructive |
| `gmail_send_draft(draft_id, account)` | alias / email | **destructive** |
| `drive_search(query, account="all", max_results=20, page_token=None)` | alias / email / `all` | read-only |
| `drive_list_recent(account="all", max_results=20, page_token=None)` | alias / email / `all` | read-only |
| `drive_read_file(file_id, account, max_chars=20000, sheet_row_cap=200)` | alias / email | read-only |
| `drive_create_file(account, name, content, mime_type="text/plain", folder_id=None)` | alias / email | write, non-destructive |

Behaviour notes:

- **Every result includes `account: {alias, email}`**: at the top level for single-account calls, and on each item for fan-out calls.
- **Fan-out (`"all"`)**: accounts are queried concurrently, then merged newest-first (Gmail by
  date, Drive by modified time). If an account fails (revoked token, API error), the others still
  return results and the failure appears in `errors: [{account, error, reconnect_url?}]`.
  `max_results` applies **per account** in fan-out mode, so pagination never skips items.
- **Pagination**: pass back `next_page_token`. For fan-out calls it's an opaque token holding every
  account's own page token. The next page only queries accounts that still have more results.
- **Archive / mark read**: `gmail_modify_labels(remove=["INBOX"])` archives and `remove=["UNREAD"]`
  marks as read. Labels can be names or IDs. `target="auto"` modifies the whole thread if the ID is a
  thread, otherwise the single message.
- **Sending**: email can only be sent from an existing draft (`gmail_create_draft` → review → `gmail_send_draft`).
- **drive_read_file**: Google Docs → Markdown, Sheets → CSV per sheet (row cap), Slides → text,
  PDFs → extracted text, text/JSON/HTML files → text. Content beyond `max_chars` is cut with
  `truncated: true`. Other types return metadata only (`supported: false`).
- **drive_search** takes plain keywords (`"quoted phrases"` supported). Every term is escaped into a
  `name contains … or fullText contains …` clause, so input can't change the query. Shared
  drives are included.
- **Revoked/expired tokens**: the account is marked `revoked`, and the error names the alias and
  email and gives the reconnect URL.

## 1. Google Cloud setup

1. Create (or pick) a project at <https://console.cloud.google.com>.
2. **Enable APIs** (APIs & Services → Library): **Gmail API**, **Google Drive API**, and
   **Google Sheets API**. Sheets is used to read every tab of a spreadsheet as CSV with the
   `drive.readonly` scope.
3. **OAuth consent screen** (Google Auth Platform → Branding / Audience / Data access):
   - User type **External** (or **Internal** if every account is in one Workspace org).
   - Add the scopes: `openid`, `.../auth/userinfo.email`, `gmail.readonly`, `gmail.compose`,
     `gmail.modify`, `drive.readonly`, `drive.file`.
   - While the app is in **Testing**, add every Google account you'll connect as a **test user**.
     Note: in Testing mode Google expires refresh tokens after **7 days**. To avoid weekly
     reconnects, set the publishing status to **In production**. For a personal app you can
     usually skip verification: you'll see an "unverified app" warning and click through.
   - Workspace accounts: the Workspace admin may need to allow the app (Admin console → Security →
     API controls → App access control).
4. **Credentials → Create credentials → OAuth client ID → Web application**.
   **Authorized redirect URIs** (add both, for each environment):
   - `https://YOUR-HOST/auth/callback` (MCP sign-in, used by FastMCP)
   - `https://YOUR-HOST/accounts/callback` (connecting data accounts)
   - For local development: `http://localhost:8080/auth/callback` and `http://localhost:8080/accounts/callback`
5. Copy the client ID and secret.

### Trimming scopes

Scopes live in one list: `GOOGLE_API_SCOPES` in [`src/multi_google_mcp/config.py`](src/multi_google_mcp/config.py).
Remove scopes you don't want, for example drop `gmail.compose` and `gmail.modify` for read-only mail.
Then reconnect the accounts. Tools that need a removed scope return a clear "missing OAuth scope" error.

## 2. Environment variables

| Variable | Required | Description |
|---|---|---|
| `GOOGLE_CLIENT_ID` | ✔ | OAuth client ID (Web application) |
| `GOOGLE_CLIENT_SECRET` | ✔ | OAuth client secret |
| `BASE_URL` | ✔ | Public URL, no trailing slash (e.g. `https://mgm.fly.dev`) |
| `TOKEN_ENC_KEY` | ✔ | Fernet key; encrypts refresh tokens, OAuth state and session cookies |
| `ALLOWED_EMAILS` | ✔ | Comma-separated Google identities allowed to use `/mcp` and `/accounts` |
| `DATA_DIR` | | Where `accounts.db` and the OAuth proxy store live (default `./data`; `/data` in Docker) |
| `JWT_SIGNING_KEY` | | Signs MCP access tokens; defaults to a key derived from the client secret |
| `MCP_ALLOWED_REDIRECT_URIS` | | Restrict which MCP clients can register, e.g. `https://claude.ai/api/mcp/auth_callback,https://claude.com/api/mcp/auth_callback` |
| `SQLITE_JOURNAL_MODE` | | `WAL` (default) or `DELETE` for network filesystems |
| `PORT` | | Listen port (default `8080`) |
| `LOG_LEVEL` | | Default `INFO` |

Generate a key:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Keep `TOKEN_ENC_KEY` safe and stable. Losing it makes the stored tokens unreadable, and you'll
have to reconnect every account.

## 3. Run locally

```bash
python3.14 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

```bash
cp .env.example .env   # fill in the values
```

```bash
set -a; source .env; set +a; .venv/bin/python -m multi_google_mcp
```

Then open <http://localhost:8080/accounts>. With an `http://localhost` `BASE_URL`, the server
allows OAuth over plain HTTP.

Tests use mocked Google APIs, so they need no network:

```bash
.venv/bin/pytest -q
```

claude.ai must reach the server over public HTTPS, so for an end-to-end test from claude.ai,
use a tunnel (e.g. `cloudflared tunnel --url http://localhost:8080`). Set `BASE_URL` to the tunnel
URL and add its two redirect URIs to the OAuth client.

## 4. Deploy

The container keeps state in `DATA_DIR` (`/data`): the SQLite DB and FastMCP's encrypted OAuth
client store. That directory **must be persistent**, or you'll have to reconnect the accounts
and re-add the connector after every restart. Run **a single instance**, because SQLite is local.

### Fly.io (recommended: simple persistent volume)

```bash
fly launch --no-deploy --copy-config
```

```bash
fly volumes create mgm_data --size 1
```

```bash
fly secrets set GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... TOKEN_ENC_KEY=... ALLOWED_EMAILS=you@gmail.com BASE_URL=https://<app>.fly.dev
```

```bash
fly deploy
```

### Google Cloud Run

Cloud Run's filesystem is ephemeral, so mount a Cloud Storage volume at `/data`, use
`SQLITE_JOURNAL_MODE=DELETE` (WAL doesn't work on network filesystems), and pin to one instance.

```bash
gcloud storage buckets create gs://YOUR-BUCKET --location=us-central1
```

```bash
gcloud run deploy multi-google-mcp --source . --region us-central1 --allow-unauthenticated --max-instances 1 --execution-environment gen2 --add-volume name=data,type=cloud-storage,bucket=YOUR-BUCKET --add-volume-mount volume=data,mount-path=/data --set-env-vars SQLITE_JOURNAL_MODE=DELETE,ALLOWED_EMAILS=you@gmail.com,BASE_URL=https://multi-google-mcp-XXXX.run.app --set-secrets GOOGLE_CLIENT_ID=gcid:latest,GOOGLE_CLIENT_SECRET=gcsecret:latest,TOKEN_ENC_KEY=tokenkey:latest
```

`--allow-unauthenticated` is correct here: the app does its own OAuth, and Cloud Run IAM auth
would block claude.ai. Deploy once to learn the URL, then set `BASE_URL` and the redirect URIs.

## 5. Connect Google accounts

1. Open `https://YOUR-HOST/accounts` and sign in with Google as an `ALLOWED_EMAILS` identity.
   This creates a 12-hour admin session cookie.
2. Enter an alias and click **Connect with Google**, or go directly to
   `https://YOUR-HOST/accounts/connect?alias=work`.
3. Choose the Google account and **tick every permission box**. Google's granular consent lets you
   untick scopes, and the success page warns if any are missing.
4. Repeat for each account. To reconnect a revoked account, open its reconnect link (the same URL
   with the same alias). To remove an account, use **disconnect**, which also revokes the token at Google.

The `/accounts` routes are protected because every action requires the admin session, and that
session can only be created by an `ALLOWED_EMAILS` Google identity. OAuth `state` is encrypted,
expires after 10 minutes and is bound to your browser by a nonce cookie. PKCE is used.

## 6. Add to claude.ai as a custom connector

1. In claude.ai go to **Settings → Connectors → Add custom connector**. On Team/Enterprise plans an
   owner adds it under organization settings.
2. Name: `Google (multi-account)`. URL: `https://YOUR-HOST/mcp`. Leave the OAuth client ID/secret
   fields empty: the server supports dynamic client registration.
3. Click **Connect**. You'll see a FastMCP consent page, then Google sign-in. Sign in as an
   `ALLOWED_EMAILS` identity.
4. Once enabled, the connector works on claude.ai web and the mobile apps.

In a chat, try: *"List my accounts"*, *"Search all my inboxes for invoices from last month"*,
*"Find the Q3 plan in my work drive and summarize it"*.

## Security & privacy

- Refresh tokens are Fernet-encrypted in SQLite. FastMCP's upstream token store is Fernet-encrypted in `DATA_DIR/oauth`.
- Email bodies, file contents and tokens are never logged. Access-log query strings (OAuth codes/state) are redacted,
  and googleapiclient's request logging is silenced.
- The allowlist is checked on **every** MCP request, not just at login.
- Tool errors are compact messages. Unexpected exceptions are masked (`mask_error_details`).
- Note: Cloud Run and Fly's own edge request logs may still record URLs. OAuth codes are single-use and short-lived.

## Layout

```
src/multi_google_mcp/
  config.py       env settings + GOOGLE_API_SCOPES (the one list to trim)
  crypto.py       Fernet encrypt/seal helpers
  store.py        SQLite AccountStore (encrypted refresh tokens, status)
  google_auth.py  credentials, auto-refresh, revoked → AccountAuthError(reconnect URL), service factory
  accounts.py     resolve alias | email | "all"
  fanout.py       parallel fan-out, merge, partial errors, composite page tokens
  text.py         HTML → text, truncation
  gmail.py        Gmail tools
  drive.py        Drive tools (safe q builder, export/extract)
  web.py          /accounts, /accounts/login, /accounts/connect, /accounts/callback, /accounts/disconnect, /healthz
  auth.py         AllowlistGoogleProvider (FastMCP GoogleProvider + ALLOWED_EMAILS)
  server.py       FastMCP server, tool registration + annotations, ASGI app
tests/            mocked Google APIs
```
