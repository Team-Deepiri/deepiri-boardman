# boardman-ui

Vite + React shell for the Board Manager agent (`/api/v1/agent/chat`). Dev server proxies `/api` → `http://127.0.0.1:8090`.

```bash
cd boardman-ui
npm install
npm run dev
```

Open http://localhost:5176 — run `deepiri-boardman` API on :8090 first.

Production build is served by nginx (see `deploy/nginx` and root `docker-compose.yml`).

Optional: `VITE_API_BASE=https://your-host` when API is on another origin.
