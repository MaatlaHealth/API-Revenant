# Deployment

> Nothing has been deployed. The configurations in this repository are untested until a deployment is actually performed and recorded at the bottom of this file.

## Topology

| Part | Host | Config file |
|---|---|---|
| Frontend (Next.js) | Netlify | `netlify.toml` |
| Backend (FastAPI + runtime) | Render web service | `render.yaml` |
| Database | Render PostgreSQL | `render.yaml` |

Render is the only supported backend target. No configuration for other backend hosts is kept in the repository.

The backend is a single long-running process because the compatibility runtime must stay available independently of the frontend. It is not deployed as serverless functions.

## Backend on Render

1. Push the repository to GitHub.
2. In Render: **New → Blueprint**, select the repository. Render reads `render.yaml` and creates the web service and database.
3. Set the variables marked `sync: false` in the dashboard:
   - `REVENANT_SECRET_KEY` — a Fernet key (see `local-development.md`).
   - `REVENANT_ADMIN_TOKEN` — a long random string.
   - `REVENANT_CORS_ORIGINS` — the Netlify site URL, e.g. `https://<site>.netlify.app`.
   - `REVENANT_PUBLIC_HOSTS` — the backend's own hostname, e.g. `<service>.onrender.com`. The management API answers only on hosts in this list, and production refuses to start without it.
4. Deploy. The start command runs `alembic upgrade head` before starting Uvicorn.
5. Verify `https://<service>.onrender.com/readyz` returns ready.

Notes:
- **One worker is a requirement, not a default.** The runtime registry, host table and credentials are held in process memory, so the first release supports exactly one process with one worker (`architecture.md` §4.5). `render.yaml` pins `--workers 1` and sets `WEB_CONCURRENCY=1`. If either is changed, or the service is started under a multi-worker supervisor, the application exits at start-up with `unsupported_multi_worker`.
- **Do not scale the service beyond one instance.** A database lease is designed to prevent more than one process serving runtime traffic, under the assumptions in `architecture.md` §4.5. A second instance waits in standby and asks the first to hand over; it is never a second server. This keeps deployments safe but is not horizontal scaling.
- **Deployments cause a brief runtime pause.** When a new instance starts, it asks the old one to hand over. The old instance stops accepting runtime requests, finishes in-flight requests (at most the request lifetime), and releases the lease; only then does the new one serve. Legacy clients see `503` with `Retry-After`, typically for a few seconds and at most about 40 seconds.
- **Request lifetime.** Every runtime request is limited to 30 seconds in total. The lease timing requires `TTL ≥ grace + request lifetime + clock margin` (45 + 30 + 15 = 90 s by default); the application refuses to start otherwise.
- **Access log.** The start command passes `--no-access-log`. Do not remove it: the server's access log prints full request URLs, which could include a query-parameter credential. Render's own request logs are outside the application's control.
- **The single-server protection has limits.** It depends on clock rates and on the database not losing lease writes, and the replacement API cannot reject a stale sender. See `architecture.md` §4.5.
- **After a crash, expect up to 90 seconds before the runtime serves again.** A process that died without releasing the lease must be waited out for the lease TTL. A normal restart or deploy releases the lease and has no such wait.
- **A database outage longer than 45 seconds stops the runtime** until the database returns (the lease grace period). Shorter interruptions do not affect proxy traffic. Both figures are configurable; see `.env.example`.
- Render's free web services sleep when idle. On wake the process restarts and startup recovery reloads every runtime whose desired state is `running`; the first request after a sleep is slow. Use a paid instance for a live judging session if cold starts matter.
- Free Render PostgreSQL databases have a limited lifetime. Check the current terms before relying on one through the judging period.
- `DATABASE_URL` from Render uses the `postgresql://` scheme; the backend normalises it to the `psycopg` driver.

## Frontend on Netlify

1. **Add new site → Import an existing project**, select the repository. `netlify.toml` sets the base directory to `frontend`.
2. Set `NEXT_PUBLIC_API_URL` to the backend's public URL.
3. Deploy, then add the resulting site URL to `REVENANT_CORS_ORIGINS` on the backend and redeploy the backend.

The admin token is **not** a Netlify variable. Operators enter it in the app.

## Production configuration checklist

- [ ] `REVENANT_ENV=production`
- [ ] `REVENANT_SECRET_KEY` and `REVENANT_ADMIN_TOKEN` set to generated values
- [ ] `REVENANT_ALLOW_PRIVATE_UPSTREAMS` unset
- [ ] `WEB_CONCURRENCY=1`, one instance, start command uses `--workers 1`
- [ ] `REVENANT_PUBLIC_HOSTS` lists the backend's own hostname(s) and nothing else
- [ ] `REVENANT_TRUST_FORWARDED_HOST` unset (set it only together with `REVENANT_TRUSTED_PROXIES`)
- [ ] A request with an unknown `Host` header returns `421`; `/api/v1` on a legacy hostname is not served
- [ ] `REVENANT_LEASE_FORCE_ACQUIRE` unset
- [ ] `/system/info` shows `lease.state: held` after start
- [ ] Runtime health shows `traces.writer: running` and zero dropped traces after a smoke test
- [ ] `REVENANT_CORS_ORIGINS` lists only the frontend origin
- [ ] `/readyz` healthy after deploy
- [ ] Demo project seeded, runtime started, then service restarted once to confirm recovery

## Hosted demonstration upstream

The production outbound guard blocks private addresses, so a hosted runtime cannot reach a NovaPay simulator on `localhost`. For a hosted demonstration, the simulator is mounted inside the backend at `/demo/novapay` and enabled by `REVENANT_ENABLE_DEMO_SIMULATOR=true`; the demo project's replacement URL is then the backend's own public HTTPS origin, which passes the guard without weakening it.

## Deployment record

| Date | Target | Commit | Result | Verified by |
|---|---|---|---|---|
| — | — | — | Not yet deployed | — |
