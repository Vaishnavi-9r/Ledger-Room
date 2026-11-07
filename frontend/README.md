# LedgerRoom frontend

The frontend is a dependency-free browser app with a small Node.js development server. The server serves the UI and proxies `/api/*` to the existing FastAPI backend, keeping browser requests same-origin.

From this directory, start the API in one terminal from the repository root:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload
```

Then start the frontend in a second terminal:

```powershell
node server.cjs
```

Open `http://127.0.0.1:5173`. Set `LEDGERROOM_BACKEND_URL` or `PORT` to override the API address or frontend port.

Authentication, registration, email verification, business workspaces, member listing, and accountant invitations use the current `/api/v1` endpoints. The backend does not currently expose shared chat or agent messaging endpoints, so the room indicates this clearly and does not invent or persist messages locally.
