# boardman-ui

Vite + React shell for the Board Manager agent (`/api/v1/agent/chat`). Dev server proxies `/api` → `http://127.0.0.1:8090`.

## Prerequisites

**Node.js 20.19+** or **22.12+** (Vite 8 / Rolldown). Node 22.11 and older 22.x releases are not supported and often fail with missing `@rolldown/binding-*` native modules.

If you use [nvm](https://github.com/nvm-sh/nvm): `nvm install` in this directory picks up `.nvmrc`.

After changing Node version, reinstall deps:

```bash
cd boardman-ui
rm -rf node_modules package-lock.json
npm install
npm run dev
```

Open http://localhost:5176 — run `deepiri-boardman` API on :8090 first.

Production build is served by nginx (see `deploy/nginx` and root `docker-compose.yml`).

Optional: `VITE_API_BASE=https://your-host` when API is on another origin.
