"""FastMCP server: tool registration, auth and HTTP app assembly."""

from __future__ import annotations

import functools
import logging
from typing import Annotated, Any, Awaitable, Callable, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import drive, gmail
from .accounts import AccountNotFoundError
from .config import MCP_PATH, Settings
from .context import AppContext
from .crypto import Crypto
from .google_auth import AccountError, GoogleBackend
from .store import AccountStore, StoreError
from .web import AccountsWeb

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Access to several Google accounts (Gmail + Google Drive) at once.
Call list_accounts first to see aliases. Every tool takes `account`: an alias
(e.g. "work"), an email address, or "all" where supported (search/list tools
fan out to every account in parallel). Every result is tagged with the account
it came from - tell the user which inbox/drive each item belongs to.
Emails are only ever sent from an existing draft: create a draft with
gmail_create_draft, show it to the user, and call gmail_send_draft only after
they confirm. If an account needs reconnecting, give the user the reconnect URL
from the error."""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

AccountArg = Annotated[str, Field(description="Account alias (e.g. 'work') or email address.")]
AccountOrAllArg = Annotated[
    str, Field(description="Account alias, email address, or 'all' to query every connected account in parallel.")
]
MaxResults = Annotated[int, Field(ge=1, le=100, description="Max results (per account when account='all').")]
PageToken = Annotated[
    str | None, Field(description="next_page_token from a previous call with the same query and account argument.")
]


def _tool_errors(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Turn expected failures into clear MCP tool errors (no stack traces)."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except (AccountError, AccountNotFoundError, StoreError, gmail.GmailError, drive.DriveError, ValueError) as e:
            raise ToolError(str(e)) from None

    return wrapper


def register_tools(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool(annotations=ToolAnnotations(title="List accounts", **READ_ONLY.model_dump(exclude={"title"})))
    async def list_accounts() -> dict[str, Any]:
        """List connected Google accounts: alias, email and connection status."""
        accounts = ctx.store.list()
        return {
            "accounts": [
                {
                    "alias": a.alias,
                    "email": a.email,
                    "status": a.status,
                    **({"reconnect_url": ctx.settings.connect_url(a.alias)} if a.status != "connected" else {}),
                }
                for a in accounts
            ],
            "manage_url": ctx.accounts_page_url,
        }

    # ------------------------------------------------------------------ Gmail
    @mcp.tool(annotations=READ_ONLY)
    @_tool_errors
    async def gmail_search(
        query: Annotated[str, Field(description="Gmail search syntax, e.g. 'from:alice is:unread newer_than:7d'.")],
        account: AccountOrAllArg = "all",
        max_results: MaxResults = 20,
        page_token: PageToken = None,
    ) -> dict[str, Any]:
        """Search Gmail messages. With account='all', searches every account in parallel and
        merges results newest-first; each message is tagged with its account. Accounts that
        fail are listed in `errors` while the others still return results."""
        return await gmail.search(ctx, query, account, max_results, page_token)

    @mcp.tool(annotations=READ_ONLY)
    @_tool_errors
    async def gmail_get_thread(
        thread_id: str,
        account: AccountArg,
        max_body_chars: Annotated[int, Field(ge=200, le=100_000, description="Per-message body limit.")] = 8000,
    ) -> dict[str, Any]:
        """Get a Gmail thread: each message's headers, plain-text body (HTML converted to text,
        long bodies truncated with body_truncated=true) and attachment names/sizes."""
        return await gmail.get_thread(ctx, thread_id, account, max_body_chars)

    @mcp.tool(annotations=READ_ONLY)
    @_tool_errors
    async def gmail_list_labels(account: AccountArg) -> dict[str, Any]:
        """List Gmail labels (system and user) for one account."""
        return await gmail.list_labels(ctx, account)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                          idempotent_hint=True, open_world_hint=True))
    @_tool_errors
    async def gmail_modify_labels(
        message_or_thread_id: str,
        account: AccountArg,
        add: Annotated[list[str], Field(description="Label names or IDs to add, e.g. ['STARRED', 'Receipts'].")] = [],  # noqa: B006
        remove: Annotated[list[str], Field(description="Label names or IDs to remove. Archive = ['INBOX'], mark read = ['UNREAD'].")] = [],  # noqa: B006
        target: Annotated[Literal["auto", "thread", "message"], Field(
            description="'auto' modifies the whole thread if the ID is a thread ID, else the single message.")] = "auto",
    ) -> dict[str, Any]:
        """Add/remove Gmail labels on a thread or message. Covers archive (remove INBOX),
        mark read (remove UNREAD), mark unread (add UNREAD), star (add STARRED)."""
        return await gmail.modify_labels(ctx, message_or_thread_id, account, add, remove, target)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                          idempotent_hint=False, open_world_hint=True))
    @_tool_errors
    async def gmail_create_draft(
        account: AccountArg,
        to: Annotated[list[str], Field(description="Recipient email addresses.")],
        subject: Annotated[str, Field(description="Subject. For replies, may be empty to reuse 'Re: <original>'.")],
        body: Annotated[str, Field(description="Plain-text body.")],
        cc: list[str] | None = None,
        reply_to_thread_id: Annotated[str | None, Field(
            description="Thread ID to reply in; sets threading headers.")] = None,
    ) -> dict[str, Any]:
        """Create a Gmail draft (does NOT send). Send it later with gmail_send_draft after
        the user confirms."""
        return await gmail.create_draft(ctx, account, to, subject, body, cc, reply_to_thread_id)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                          idempotent_hint=False, open_world_hint=True))
    @_tool_errors
    async def gmail_send_draft(draft_id: str, account: AccountArg) -> dict[str, Any]:
        """Send an existing Gmail draft. Irreversible: only call after the user has
        reviewed the draft and explicitly confirmed sending it."""
        return await gmail.send_draft(ctx, draft_id, account)

    # ------------------------------------------------------------------ Drive
    @mcp.tool(annotations=READ_ONLY)
    @_tool_errors
    async def drive_search(
        query: Annotated[str, Field(description="Plain keywords; use \"double quotes\" for phrases. Matches names and content.")],
        account: AccountOrAllArg = "all",
        max_results: MaxResults = 20,
        page_token: PageToken = None,
    ) -> dict[str, Any]:
        """Search Google Drive files (My Drive, shared with me and shared drives) by keyword.
        With account='all', searches every account in parallel."""
        return await drive.search(ctx, query, account, max_results, page_token)

    @mcp.tool(annotations=READ_ONLY)
    @_tool_errors
    async def drive_list_recent(
        account: AccountOrAllArg = "all",
        max_results: MaxResults = 20,
        page_token: PageToken = None,
    ) -> dict[str, Any]:
        """List recently modified Drive files, newest first."""
        return await drive.list_recent(ctx, account, max_results, page_token)

    @mcp.tool(annotations=READ_ONLY)
    @_tool_errors
    async def drive_read_file(
        file_id: str,
        account: AccountArg,
        max_chars: Annotated[int, Field(ge=500, le=200_000, description="Content character limit.")] = 20_000,
        sheet_row_cap: Annotated[int, Field(ge=1, le=5000, description="Max rows per sheet for spreadsheets.")] = 200,
    ) -> dict[str, Any]:
        """Read a Drive file as text: Google Docs -> markdown, Sheets -> CSV per sheet (row cap),
        Slides -> text, PDFs and text files -> extracted text. Long content is truncated
        (truncated=true). Unsupported types return metadata only."""
        return await drive.read_file(ctx, file_id, account, max_chars, sheet_row_cap)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                          idempotent_hint=False, open_world_hint=True))
    @_tool_errors
    async def drive_create_file(
        account: AccountArg,
        name: str,
        content: str,
        mime_type: Annotated[str, Field(description=(
            "text/plain, text/markdown, text/csv, text/html, application/json; or "
            "application/vnd.google-apps.document / application/vnd.google-apps.spreadsheet "
            "to convert to a Google Doc/Sheet."))] = "text/plain",
        folder_id: Annotated[str | None, Field(description="Parent folder ID (default: My Drive root).")] = None,
    ) -> dict[str, Any]:
        """Create a new file in Google Drive. Never overwrites existing files."""
        return await drive.create_file(ctx, account, name, content, mime_type, folder_id)


def _quiet_loggers() -> None:
    # googleapiclient logs request URIs (which can include search queries) at DEBUG/INFO.
    for name in ("googleapiclient.discovery", "googleapiclient.http", "googleapiclient.discovery_cache",
                 "google_auth_oauthlib", "oauthlib", "requests_oauthlib", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def create_app(settings: Settings | None = None, *, auth: Any = "default"):
    """Build the ASGI app. `auth=None` disables MCP auth (tests only)."""
    from .auth import build_auth_provider

    settings = settings or Settings.from_env()
    _quiet_loggers()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    crypto = Crypto(settings.token_enc_key)
    store = AccountStore(settings.db_path, crypto, journal_mode=settings.sqlite_journal_mode)
    backend = GoogleBackend(settings, store)
    ctx = AppContext(settings=settings, store=store, google=backend)

    provider = build_auth_provider(settings) if auth == "default" else auth
    mcp = FastMCP("Multi-account Google", instructions=INSTRUCTIONS, auth=provider, mask_error_details=True)
    register_tools(mcp, ctx)

    web = AccountsWeb(settings, store, backend, crypto)
    mcp.custom_route("/accounts", methods=["GET"])(web.index)
    mcp.custom_route("/accounts/login", methods=["GET"])(web.login)
    mcp.custom_route("/accounts/connect", methods=["GET"])(web.connect)
    mcp.custom_route("/accounts/callback", methods=["GET"])(web.callback)
    mcp.custom_route("/accounts/disconnect", methods=["POST"])(web.disconnect)
    mcp.custom_route("/healthz", methods=["GET"])(web.healthz)

    app = mcp.http_app(path=MCP_PATH, stateless_http=True)
    return app, mcp, ctx
