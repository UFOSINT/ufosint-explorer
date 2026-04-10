# Deployment

How the GitHub → Azure pipeline works, plus the one-time setup you need
to reproduce it in a fresh Azure subscription.

## 1. Hosting at a glance

| Resource | SKU | Monthly cost (Apr 2026) | Notes |
|----------|-----|-------------------------|-------|
| Azure App Service Plan (Linux) | B1 | ~$13 | 1.75 GB RAM, 1 vCPU |
| Azure App Service (ufosint-explorer) | — | included | Python 3.12 runtime |
| Azure Database for PostgreSQL Flexible Server | Burstable B1ms | ~$15 | 2 GB RAM, 1 vCore, 32 GB storage |
| **Total**                      |     | **~$28/mo**             | |

Resource group: `rg-ufosint-prod`. Region: `centralus`.

## 2. One-time setup

### 2.1 Azure resources

```bash
# Log in as the account that owns the TTX subscription
az login
az account set --subscription "<subscription-id>"

# Register the Postgres provider (first-time accounts only)
az provider register --namespace Microsoft.DBforPostgreSQL --wait

# Resource group
az group create --name rg-ufosint-prod --location centralus

# App Service plan
az appservice plan create \
  --name asp-ufosint-prod \
  --resource-group rg-ufosint-prod \
  --sku B1 --is-linux

# Web app
az webapp create \
  --name ufosint-explorer \
  --plan asp-ufosint-prod \
  --resource-group rg-ufosint-prod \
  --runtime "PYTHON:3.12"

# PostgreSQL Flexible Server
az postgres flexible-server create \
  --name ufosint-pg \
  --resource-group rg-ufosint-prod \
  --location centralus \
  --tier Burstable --sku-name Standard_B1ms \
  --storage-size 32 \
  --version 16 \
  --admin-user ufosintadmin \
  --admin-password '<generated>' \
  --public-access 0.0.0.0
  # ^ allows all Azure services; tighten this if you're worried

# Allow pg_trgm extension (needed by some indexes in pg_schema.sql)
az postgres flexible-server parameter set \
  --resource-group rg-ufosint-prod \
  --server-name ufosint-pg \
  --name azure.extensions --value pg_trgm

# Create the database
az postgres flexible-server db create \
  --resource-group rg-ufosint-prod \
  --server-name ufosint-pg \
  --database-name ufo_unified
```

### 2.2 Load the schema and data

```bash
# Connect to the flexible server via psql
export PGPASSWORD='<admin-password>'
psql "host=ufosint-pg.postgres.database.azure.com \
      user=ufosintadmin dbname=ufo_unified sslmode=require" \
  < scripts/pg_schema.sql

# Run the migration from the canonical SQLite snapshot
# (see ufo-dedup repo for how that snapshot is built)
DATABASE_URL="postgresql://ufosintadmin:<password>@ufosint-pg.postgres.database.azure.com:5432/ufo_unified?sslmode=require" \
SQLITE_PATH=../ufo-dedup/output/ufo_unified.db \
python scripts/migrate_sqlite_to_pg.py
```

The migration takes ~5 minutes. Uploads 614,505 sightings, 126,730
duplicate candidates, and the enrichment tables.

### 2.3 App Service configuration

```bash
az webapp config appsettings set \
  --name ufosint-explorer \
  --resource-group rg-ufosint-prod \
  --settings \
    DATABASE_URL="postgresql://ufosintadmin:<password>@ufosint-pg.postgres.database.azure.com:5432/ufo_unified?sslmode=require" \
    SCM_DO_BUILD_DURING_DEPLOYMENT=true
```

**Gotcha**: if you're running the `az` command from git-bash on
Windows, prefix with `MSYS_NO_PATHCONV=1` or the `/` in the URL will be
mangled into `C:/Program Files/Git/...`. Bit me once already — see the
commit history around `4653856`.

### 2.4 GitHub Actions publish profile

```bash
# Enable basic-auth publishing credentials (disabled by default on new apps)
az resource update \
  --resource-group rg-ufosint-prod \
  --name scm --namespace Microsoft.Web \
  --resource-type basicPublishingCredentialsPolicies \
  --parent sites/ufosint-explorer \
  --set properties.allow=true

az resource update \
  --resource-group rg-ufosint-prod \
  --name ftp --namespace Microsoft.Web \
  --resource-type basicPublishingCredentialsPolicies \
  --parent sites/ufosint-explorer \
  --set properties.allow=true

# Download the publish profile XML
az webapp deployment list-publishing-profiles \
  --name ufosint-explorer \
  --resource-group rg-ufosint-prod \
  --xml > publish-profile.xml
```

Paste the XML into GitHub: repo Settings → Secrets and variables →
Actions → New repository secret → Name: `AZUREAPPSERVICE_PUBLISHPROFILE`,
Value: full XML contents.

## 3. CI/CD pipeline

`.github/workflows/azure-deploy.yml` has four jobs:

```
          ┌───────┐
push  ──▶ │ test  │ ──▶ ┌───────┐ ──▶ ┌────────┐ ──▶ ┌───────┐
tag   ──▶ │       │     │ build │     │ deploy │     │ smoke │
manual─▶ │       │     │       │     │        │     │       │
          └───────┘     └───────┘     └────────┘     └───────┘
```

| Job     | What it does                                              | Fails if                                              |
|---------|-----------------------------------------------------------|-------------------------------------------------------|
| test    | ruff check, node -c static/app.js, pytest                 | any lint error, JS syntax error, or test failure      |
| build   | pip install + zip everything except tests/, .git, venv    | install fails                                         |
| deploy  | azure/webapps-deploy@v3 pushes the zip                    | publish-profile invalid or Azure rejects the upload   |
| smoke   | curls `/health`, `/`, `/api/filters`, `/api/stats`        | `/health` ≠ ok, HTML missing `?v=`, API endpoints 5xx |

The smoke job is what would have caught the Sprint 4 stale-cache bug
had it existed. It waits 45s for the container to restart, then checks
that the rendered HTML contains `style.css?v=` and `app.js?v=` — if
the cache-bust pattern disappears from the HTML, the deploy is marked
failed.

### What triggers a deploy

- `push` to `main`
- `push` of a tag matching `v*`
- Manual `workflow_dispatch` from the Actions tab

### Rollback

There's no automated rollback. If a deploy ships a broken commit:

```bash
# Option A: revert the offending commit
git revert <bad-sha>
git push origin main

# Option B: hard-reset main to a known-good tag
git reset --hard v0.4.0
git push --force-with-lease origin main
```

Option B is destructive — prefer A unless the bad commit has
information you need to discard. Never force-push to `main` without
checking Slack with a human first.

## 4. Versioning

The `CHANGELOG.md` at the repo root documents every release. We use
SemVer (`v<major>.<minor>.<patch>`):

- **MAJOR** reserved for the `ufosint.com` cutover + stable public API
- **MINOR** per sprint / coherent feature set
- **PATCH** for bugfixes

### Cutting a release

```bash
# 1. Make sure CHANGELOG.md has an entry for the version you're cutting,
#    moved out of [Unreleased]
# 2. Commit any pending changes
git commit -am "Prep v0.4.1 release"
# 3. Tag it
git tag -a v0.4.1 -m "v0.4.1 — stale-cache hotfix + test suite"
# 4. Push the commit + tag
git push origin main
git push origin v0.4.1
```

Pushing the tag triggers the workflow (because `on: push: tags: 'v*'`),
so the tagged commit gets deployed automatically.

## 5. Environment variables reference

| Variable | Where set | Required? | Purpose |
|----------|-----------|-----------|---------|
| `DATABASE_URL` | App Service → Configuration → Application settings | **Yes** | psycopg connection string. Must include `sslmode=require`. App will refuse to start without it. |
| `ASSET_VERSION` | (none — let it auto-compute) | No | Override the auto-computed asset version. Only set this if you're debugging the versioning system itself. |
| `GITHUB_SHA` | Set automatically by Actions | No | Fallback for `ASSET_VERSION` when the app is running on a runner. |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | App Service → Configuration | **Yes** (`true`) | Tells App Service to run `pip install -r requirements.txt` on deploy. |
| `PORT` | Set automatically by App Service | No | gunicorn's bind port. Procfile reads `$PORT`. |
| `WEBSITES_PORT` | (unused) | No | Only needed for custom container scenarios. |
| `REDIS_URL` | App Service → Configuration → Application settings | No | When set, Flask-Caching uses RedisCache (shared across gunicorn workers). When unset, each worker keeps its own SimpleCache. Format: `rediss://:<PRIMARY_KEY>@<name>.redis.cache.windows.net:6380/0`. See section 7.3. |

## 6. Monitoring & debugging the live site

```bash
# Tail the app logs
az webapp log tail \
  --name ufosint-explorer \
  --resource-group rg-ufosint-prod

# One-shot health check
curl -fsS https://ufosint-explorer.azurewebsites.net/health

# Check which ASSET_VERSION is currently shipping
curl -sS https://ufosint-explorer.azurewebsites.net/ \
  | grep -oE 'style\.css\?v=[a-f0-9]+' | head -1
```

If the site is down, check in this order:

1. `az webapp log tail` — look for startup errors
2. `/health` — returns sightings count and "ok" when the pool is up
3. PostgreSQL firewall — if the DB restarted, the App Service IP may
   need to be re-added to the allow list
4. Latest Actions run — a failed deploy may have left the container in
   a half-restarted state

## 7. Performance tuning

Free and cheap knobs to make the site snappier without scaling the App
Service plan up. Apply these in order — the earlier entries are free
and give most of the benefit.

### 7.1 Postgres server parameters (free, 10 minutes)

Open **Azure portal → `ufosint-pg` Flexible Server → Server parameters**
and set the values below. The reference numbers are tuned for the
current B1ms tier (2 GB RAM); multiply by 4 for B2ms (8 GB).

| Parameter | B1ms value | Why |
|-----------|-----------|-----|
| `shared_buffers` | `768MB` | ~30% of RAM resident for PG pages |
| `effective_cache_size` | `1500MB` | Tells planner the OS page cache is big |
| `work_mem` | `16MB` | Sort/hash per op — bigger than default 4MB |
| `maintenance_work_mem` | `128MB` | Faster VACUUM + CREATE INDEX |
| `random_page_cost` | `1.1` | SSD, not spinning rust (default is 4) |
| `effective_io_concurrency` | `200` | SSD parallel-read hint |
| `default_statistics_target` | `200` | Better histograms on skewed cols |
| `jit` | `off` | JIT startup cost > benefit for our query mix |

`shared_buffers` is a restart parameter (Azure schedules it), the rest
apply on save. Verify with:

```bash
psql "$DATABASE_URL" -c "SHOW shared_buffers; SHOW work_mem;"
```

### 7.2 pg_prewarm (free, one restart)

After changing `shared_buffers` the cache is empty, so the first
request to every endpoint eats disk I/O. Enable the `pg_prewarm`
extension (one time) and run the helper script to preload the hot
tables + indexes:

```bash
# One-time: enable the extension on the flexible server
az postgres flexible-server parameter set \
  --resource-group rg-ufosint-prod \
  --server-name ufosint-pg \
  --name azure.extensions --value pg_trgm,pg_prewarm

# Then, on each restart, run:
psql "$DATABASE_URL" -f scripts/pg_tuning.sql
```

The app also calls `pg_prewarm` on worker boot (`_pg_prewarm_relations`
in `app.py`), so a deploy-triggered restart is self-healing. The
`scripts/pg_tuning.sql` path is just for manual ops work after a
database-side restart.

### 7.3 Azure Cache for Redis (~$16/mo)

`SimpleCache` is per-gunicorn-worker, so a cached response only helps
the worker that computed it. A shared Redis cache means the FIRST hit
on any worker warms the cache for every other worker, and the cache
survives worker restarts.

```bash
# Create a Basic C0 instance (250 MB, plenty for our workload)
az redis create \
  --name ufosint-cache \
  --resource-group rg-ufosint-prod \
  --location centralus \
  --sku Basic --vm-size c0

# Grab the primary key
az redis list-keys \
  --name ufosint-cache \
  --resource-group rg-ufosint-prod

# Wire it into the App Service (note: rediss:// for TLS on port 6380)
az webapp config appsettings set \
  --name ufosint-explorer \
  --resource-group rg-ufosint-prod \
  --settings \
    REDIS_URL="rediss://:<PRIMARY_KEY>@ufosint-cache.redis.cache.windows.net:6380/0"
```

The app auto-detects `REDIS_URL` at startup and switches Flask-Caching
to `RedisCache` with a versioned key prefix (`ufosint:<ASSET_VERSION>:`)
so deploys auto-invalidate their predecessor's cache. Boot logs will
show `[cache] backend=RedisCache prefix=...`. Unset the env var to
revert to per-worker SimpleCache.

### 7.4 When to scale up (paid)

If 7.1–7.3 aren't enough:

1. **Postgres B1ms → B2ms** (~$15 → ~$45/mo). 4× the RAM means the
   whole working set is resident. Biggest single win for query
   latency. Change the `--tier`/`--sku-name` via `az postgres
   flexible-server update`.
2. **App Service B1 → B2** (~$13 → ~$26/mo). Only worth it if
   `az webapp log tail` shows gunicorn workers saturated. More likely
   to help concurrency than single-request latency.
3. **App Service → P0v3/P1v3** (~$55+/mo). Enables deployment slots
   (real staging + blue/green) and much faster per-core CPU. Revisit
   when traffic justifies it.

## 8. Future work

- **Custom domain `ufosint.com`**: add via App Service → Custom
  domains, validate with DNS TXT + A records, enable App Service
  Managed Certificate for free HTTPS. Blocked on weekend DNS work with
  the collaborator who owns the domain.
- **Staging slot**: App Service supports deployment slots at B2+; a
  staging slot on `asp-ufosint-prod` would let us run the smoke job
  against staging first and swap on green. Costs one extra plan tier
  bump (~$13/mo) so deferred for now.
- **Replace the publish profile with an OIDC federated credential**:
  longer-lived, no secret rotation. GitHub's azure/login@v2 supports
  this. Low-priority since the publish profile isn't customer-facing.
