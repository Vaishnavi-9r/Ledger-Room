# LedgerRoom MCP server

This local MCP server registers two clarification tool names:

- `get_pending_clarifications()` calls the existing `GET /api/v1/clarifications/`
  endpoint with `status=pending` and returns the request ID, bank-line ID, bank
  description, transaction date, amount in paise, and status.
- `respond_to_clarification(request_id, owner_response)` submits the owner's
  provided explanation through the existing `POST
  /api/v1/clarifications/{request_id}/respond` endpoint. It records the response
  only; it does not mark an invoice paid, match a bank transaction, or resolve
  the clarification.

**Clarification operations currently fail closed.** The configured Switch Core
0.31.0 stdio MCP tool handler does not receive the authenticated Mattermost
sender or channel. The tools return `identity_unavailable` and make no backend
request. Switch's own Mattermost identity claim is a separate control used for
Switch agent addressing: the human must claim their Mattermost account for
their own Switch user in Switch Console under **Messaging apps**. That claim
does not authenticate LedgerRoom MCP calls and does not replace LedgerRoom's
OAuth identity or business membership checks.

Do not add identity as a tool argument or place an owner JWT in MCP
configuration. The client functions in `backend_client.py` remain isolated
HTTP helpers and do not authenticate requests; they must only be used after a
trusted per-message identity delegation mechanism is available to the MCP
handler and independently validated by LedgerRoom.

## Install

From the LedgerRoom root in PowerShell:

```powershell
& .\.venv\Scripts\python.exe -m pip install -r .\mcp_server\requirements.txt
```

## Run locally

Start the FastAPI backend separately, bound to loopback, for example:

```powershell
Set-Location C:\LedgerRoom\backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then launch the MCP server in another terminal:

```powershell
Set-Location C:\LedgerRoom
& .\.venv\Scripts\python.exe .\mcp_server\server.py
```

The server uses stdio, so it waits for an MCP host to send protocol messages;
it does not print a startup banner. Do not add `print()` calls to the server:
stdout carries the MCP protocol.

## Test

Run the isolated HTTP mock tests without contacting Supabase or the backend:

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s .\mcp_server\tests -v
```

For an interactive protocol check, the official MCP Inspector can launch the
server over stdio. The Inspector requires Node.js / `npx`:

```powershell
npx -y @modelcontextprotocol/inspector C:\LedgerRoom\.venv\Scripts\python.exe C:\LedgerRoom\mcp_server\server.py
```

The Inspector can confirm tool discovery and the safe
`identity_unavailable` response. A successful backend operation requires a
trusted session identity bridge that the current Switch integration does not
provide to this stdio server.
