# QIS dashboard

Admin web UI for Qwen Image Service. React 19 + Vite + TypeScript + Tailwind CSS v4, TanStack Query, react-router, Recharts.
The gateway serves the built files at `/admin/` (any `/admin/*` path falls back to `index.html`).

## Develop

```bash
cd web
npm install
npm run dev          # http://localhost:5173/admin/
```

The dev server proxies `/admin/api`, `/v1`, `/healthz`, `/readyz`, `/docs` to a running gateway
(default `http://127.0.0.1:8080`; override with `QIS_URL=http://127.0.0.1:8090 npm run dev`).
The proxy rewrites the `Origin` header so the gateway's CSRF origin check accepts writes.

You need a dashboard user: `qis admin create --username admin` on the node (or the first-run form on localhost).

## Build

```bash
npm run build        # typecheck + bundle into ../qis/web/dist (emptied first)
npm run typecheck    # tsc --noEmit (strict)
npm run lint         # eslint
npm test -- --run    # vitest (api client, schema form, snippets, SSE helpers)
```

`qis/web/dist` is what the Python package ships; rebuild it after changing anything here.

## Layout

- `src/lib/api.ts` – fetch wrapper: JSON, `X-CSRF-Token` on writes, same-origin cookies, `ApiError` from the gateway's `{"error": {...}}` body; 401 / `must_change_password` switch the app to the login / change-password screen.
- `src/lib/auth.tsx` – session restore (`GET /admin/api/auth/me`), login, first-admin setup, logout, role checks (admin > operator > viewer).
- `src/lib/live.tsx` – one `EventSource('/admin/api/events')`: node status, job progress, model downloads.
- `src/lib/queries.ts` – TanStack Query hooks per endpoint.
- `src/lib/schema.ts` + `src/components/SchemaForm.tsx` – settings forms generated from the pydantic JSON schema.
- `src/lib/snippets.ts` – "copy as curl / Python" for playground requests.
- `src/pages/*` – one file per page, lazy-loaded.
