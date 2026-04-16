# Operations

Runbook for keeping `ufosint-explorer` alive in production. Covers
the Azure infrastructure we depend on, known failure modes with
their mitigations, and a short incident log.

Pairs with [`DEPLOYMENT.md`](DEPLOYMENT.md) (how code gets to prod)
and [`ARCHITECTURE.md`](ARCHITECTURE.md) (how the app is wired).

## 1. Production footprint

| Resource                       | Azure name                  | Region / tier                     |
|--------------------------------|-----------------------------|-----------------------------------|
| App Service (prod)             | `ufosint-explorer`          | Linux, B1                         |
| App Service (staging)          | `ufosint-explorer-staging`  | Linux, B1 — shares prod's PG      |
| PostgreSQL Flexible Server     | `ufosint-pg`                | Burstable B1ms, PG 16, 32 GB      |
| Resource group                 | `rg-ufosint-prod`           | —                                 |

Public URL: `https://ufosint.com` → `ufosint-explorer.azurewebsites.net`
Staging URL: `https://ufosint-explorer-staging.azurewebsites.net`

## 2. Known failure modes + mitigations

### 2.1 Wedged connection pool (`PoolTimeout`)

**Symptom.** All `/api/*` endpoints return HTTP 500 with
`psycopg_pool.PoolTimeout: couldn't get a connection after 30.00
sec` in the logs. `/health` may still return 200 during the
cold-start grace window (see §3).

**Cause.** Azure's network path silently drops long-idle PG
connections. If the pool is full of sockets that have already been
torn down server-side but still look open client-side, every
`getconn()` hands out a zombie, the query hangs, the thread stays
busy, and new requests wait 30 s then time out. Once all 8 pool
slots are wedged this way the app effectively stops serving even
though Azure still thinks it's healthy.

**Prevention (shipped in v0.12.1 + hardened in v0.12.2).** The pool
now combines socket-health checks, idle/lifetime recycling, and
fail-fast timeouts — [`app.py:170`](../app.py):

```python
_pool = ConnectionPool(
    DATABASE_URL, min_size=1, max_size=8, open=True,
    timeout=8,          # v0.12.2: was 30 — fail fast to the LB
    max_idle=300,       # 5 min — close idle conns before Azure kills them
    max_lifetime=3600,  # 1 hour — recycle as defense in depth
    check=ConnectionPool.check_connection,  # SELECT 1 before handout
    kwargs={
        "autocommit": True,
        "connect_timeout": 5,                       # v0.12.2
        "options": ("-c default_transaction_read_only=on "
                    "-c statement_timeout=25000"),  # v0.12.2
    },
)
```

- `check` is self-healing: dead connections get discarded and replaced
  instead of handed out and wedging the caller.
- `timeout=8` + `/health` returning 503 on DB failure (v0.12.2) lets
  Azure App Service Health Check auto-restart a wedged instance
  instead of relying on manual `az webapp restart`.
- `connect_timeout=5` caps how long opening a *fresh* connection can
  block. Without it, the pool refilling after a mass eviction can
  stall indefinitely on a dead NAT path.
- `statement_timeout=25000` is the server-side kill switch for any
  single runaway query.

**Mitigation if it happens anyway.** Restart the App Service:

```bash
az webapp restart --name ufosint-explorer --resource-group rg-ufosint-prod
```

Then wait ~60 s and verify:

```bash
curl -fsS https://ufosint.com/health           # {"status":"ok","sightings":614505}
curl -fsS -o /dev/null -w "%{http_code}\n" \
     https://ufosint.com/api/stats             # 200
```

### 2.2 PG server paused (Burstable tier)

**Symptom.** Intermittent connection refused / timeouts from the
app. `az postgres flexible-server show` reports `"state":
"Stopped"` or `"Disabled"`.

**Cause.** Burstable B1ms stops under Azure's cost-saving policies
if the subscription hits its budget guard, or a manual stop is
issued and not resumed.

**Mitigation.**

```bash
az postgres flexible-server start \
    --name ufosint-pg --resource-group rg-ufosint-prod
# wait ~60 s for PG to boot
az webapp restart --name ufosint-explorer --resource-group rg-ufosint-prod
```

The App Service restart is needed because the pool's existing
connections were closed when PG stopped; without the restart, the
pool's self-healing (§2.1) catches up eventually, but the restart
is faster.

### 2.3 Prod deploy failed mid-rollout

**Symptom.** GitHub Actions workflow shows failure, site may be
partially updated. `gh run list --workflow azure-deploy.yml`
shows the failed run.

**Mitigation.** Re-run the failed deploy:

```bash
cd ufosint-explorer
gh run list --workflow azure-deploy.yml --limit 1
gh run rerun <run-id>
```

Or roll back by resetting `main` to the previous good SHA and
pushing (only if the failure left the app unusable — the smoke
stage in the workflow should catch most breakage before deploy
completes).

## 3. `/health` endpoint behavior

`/health` (`app.py:442`) runs `SELECT COUNT(*) FROM sighting` and
returns:

- `HTTP 200 {"status":"ok","sightings":N}` — healthy, DB reachable
- `HTTP 503 {"status":"unhealthy","detail":"..."}` — DB unreachable
  (v0.12.2 — was 200 with `status:"waiting"` prior to 2026-04-16
  hotfix, which prevented Azure from detecting a wedged worker).

**Azure App Service Health Check** is enabled against this path
(10-min LB threshold). Non-2xx responses start the eviction clock;
the LB stops routing traffic to the unhealthy instance, and after
~1h of sustained failure the instance is replaced. On single-
instance B1 we don't get replacement, but we do get auto-restart
via the platform — zero-touch recovery from the v0.12.1/2.1 wedge.

Verify it's on:

```bash
az webapp config show --name ufosint-explorer \
    --resource-group rg-ufosint-prod \
    --query "{healthCheckPath:healthCheckPath}" -o json
```

## 4. Incident log

### 2026-04-16 — prod wedged for ~8 h after v0.12 deploy

- **04:14 UTC.** v0.12.0 deploy completes. Smoke tests pass;
  `/health` returns live DB counts.
- **~05–12 UTC.** No traffic overnight. Pool connections go idle;
  Azure's network drops them server-side but the pool still
  considers them alive.
- **12:42 UTC.** First user traffic. `getconn()` hands out a
  zombie, query hangs 30 s, `PoolTimeout` raised. Every
  subsequent request queues behind the same problem. All
  `/api/*` endpoints return 500.
- **13:12 UTC.** User reports the site is stuck booting. Logs
  show `psycopg_pool.PoolTimeout: couldn't get a connection
  after 30.00 sec` on `/api/stats`, `/api/overlay`,
  `/api/timeline`, `/api/points-bulk`.
- **13:13 UTC.** `az webapp restart` issued. App Service
  recycles, pool reinitialized.
- **13:14 UTC.** `/health` returns 200 with live counts. Full
  `/api/*` surface verified green.
- **Post-mortem.** No code regression — `get_db()` /
  `ConnectionPool` setup had been unchanged for weeks. Root cause
  was the absence of pool-health parameters, making us vulnerable
  to Azure's silent TCP drop during any idle window longer than
  a few minutes.
- **Fix shipped.** `check` / `max_idle` / `max_lifetime`
  parameters added to the pool constructor (see §2.1). Tests
  updated at [`tests/conftest.py`](../tests/conftest.py) to
  stub the new `check_connection` staticmethod on `_FakePool`.

### 2026-04-16 (round 2) — prod wedged again, pool check hung on dead NAT

- **18:24 UTC.** `/api/map` starts returning 500 with
  `PoolTimeout`. Same symptom as round 1. Layer 1's
  `check=SELECT 1` apparently hung on the half-dead socket until
  OS timeout rather than failing fast.
- **18:25 UTC.** `az webapp restart` issued, pool reinitialized.
- **Post-mortem.** `check` is necessary but insufficient: when
  Azure's path silently drops packets (no RST), `SELECT 1` just
  hangs on the dead FD. Also, `/health` had been returning 200
  on failure, so Azure App Service Health Check couldn't have
  saved us even if enabled.
- **Fix shipped (v0.12.2).**
  - `/health` now returns 503 on DB failure (happy path
    unchanged so deploy smoke still works).
  - Pool `timeout=30 → 8` — return 503 to the LB in 8 s.
  - `connect_timeout=5` on the psycopg kwargs — cap fresh-
    connection dials.
  - `statement_timeout=25000` in the PG options — server-side
    kill for any stuck query.
  - Azure App Service Health Check enabled at `/health`.

## 5. Useful commands

```bash
# --- Status ---
az webapp show --name ufosint-explorer --resource-group rg-ufosint-prod \
    --query "{state:state, hostNames:hostNames}" -o json
az postgres flexible-server show --name ufosint-pg \
    --resource-group rg-ufosint-prod --query "{state:state, tier:sku.tier}" -o json

# --- Logs (tail) ---
az webapp log tail --name ufosint-explorer --resource-group rg-ufosint-prod

# --- Restart ---
az webapp restart --name ufosint-explorer --resource-group rg-ufosint-prod

# --- Deploy history (last 5) ---
gh run list --workflow azure-deploy.yml --limit 5

# --- End-to-end smoke from a shell ---
for ep in /health /api/stats /api/filters /api/overlay "/api/points-bulk?meta=1"; do
    code=$(curl -sS -m 20 -o /dev/null -w "%{http_code}" "https://ufosint.com${ep}")
    echo "${ep} -> ${code}"
done
```

## 6. Future work worth considering

- **Azure Application Insights alerting** on 5xx rate spike. Cuts
  MTTR from hours to minutes by paging someone on detection.
- **`Always On` setting** on the App Service — prevents the
  container itself from idling. Check if already enabled.
- **Observability dashboard** in Azure portal pinning the 3–4
  metrics that matter: request rate, 5xx rate, PG active
  connections, App Service CPU.
- **Scale out to 2 instances** on Standard tier — Health Check
  eviction is much more effective with a sibling to route traffic
  to while the unhealthy instance restarts.

None of the above is required now — v0.12.2's fail-fast pool +
503 health check + Azure auto-restart covers the wedging modes
observed so far. Revisit if something novel breaks.
