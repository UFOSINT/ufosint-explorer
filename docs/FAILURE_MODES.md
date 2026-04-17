# Failure Modes — Code Audit 2026-04-17

In-depth audit of `app.py` (3,442 lines) conducted after the three
pool-wedge incidents on 2026-04-16/17. The goal: find potential future
failures **before** they wake us up at 3 a.m.

**11 findings.** Each includes what breaks, the trigger, the blast
radius, and a concrete fix. Prioritised by likelihood × impact.

| # | Severity | Title | Fix effort |
|---|----------|-------|-----------|
| 1 | **CRIT** | Connection leak on exception in 10+ routes | 30 min |
| 2 | **CRIT** | `/api/overlay` leaks a connection on **every** cache miss | 5 min |
| 3 | **CRIT** | Unvalidated `float(request.args)` crashes → connection leak | 15 min |
| 4 | HIGH | Stale prewarm lockfile after worker crash | 20 min |
| 5 | HIGH | `_points_bulk_build` lock has no timeout | 10 min |
| 6 | HIGH | gunicorn timeout (180s) is 22× the pool timeout (8s) | 5 min |
| 7 | MED | No rate limiting — `/api/map?limit=100000` is a DoS amplifier | 2 h |
| 8 | MED | `FILTER_CACHE` never refreshed after startup | 30 min |
| 9 | MED | Pool size = concurrent slots, no safety margin for prewarm | 10 min |
| 10 | MED | No client-side error state when API returns 5xx | 2–3 h |
| 11 | LOW | Shared PG between staging + prod = coupled blast radius | 1 day |

---

## CRIT-1: Connection leak on exception in 10+ routes

### What breaks
The idiomatic pattern in this codebase is:

```python
conn = get_db()
cur = conn.cursor()
cur.execute(...)
# ... work that can raise ...
conn.close()    # only reached on the happy path
```

If **any** line between `get_db()` and `conn.close()` raises, the
connection is never returned to the pool. After 8 leaks, the pool is
permanently exhausted and every subsequent request gets `PoolTimeout`.

### Affected routes (not exhaustive)

| Line | Route | Has try/finally? |
|------|-------|------------------|
| 350 | `init_filters()` | ❌ No |
| 1126 | `/api/map` | ❌ No |
| 1295 | `/api/heatmap` | ❌ No |
| 1451 | `/api/hexbin` | ❌ No |
| 1777 | (pre-bulk column probe) | ❌ No |
| 2427 | `/api/timeline` | ❌ No |
| 3009 | `/api/sentiment/overview` | ❌ No |
| 3065 | `/api/sentiment/timeline` | ❌ No |
| 3114 | `/api/sentiment/by-source` | ❌ No |
| 3155 | `/api/sentiment/by-shape` | ❌ No |

Routes that **do** have `try/finally` (safe): `/health` (770),
`/api/stats` (770), `/api/points-bulk` (1909), several others at
1909, 2679, 2712, 2850.

### Trigger

Any of:
- PostgreSQL hiccup mid-query (statement_timeout fires, network blip)
- `fetchall()` on a huge result set triggers `MemoryError`
- User sends bad input (see CRIT-3 below)
- Bug in helper functions (`add_common_filters`, etc.)
- Python runtime error in the payload-construction code

### Blast radius

**After 8 exceptions on unsafe routes, pool is dead.** Every request
hangs 8s then returns 503. Azure Health Check kicks in after 5 min.
Recovery only via App Service restart.

This is the most likely cause of a **fourth** wedge — any new bug in a
payload-building code path becomes an outage, not a 500 on one request.

### Fix

Wrap every route in `try/finally`:

```python
conn = get_db()
try:
    cur = conn.cursor()
    # ... all the work ...
    return jsonify(payload)
finally:
    conn.close()
```

Or better: make `get_db()` usable as a context manager and migrate all
callers. The `_PooledConn` class already has `__enter__` / `__exit__`;
we just need to change the call sites from:

```python
conn = get_db()
# ...
conn.close()
```

to:

```python
with get_db() as conn:
    # ...
```

**Will be fixed in this audit's follow-up commit (CRIT-1 batch).**

---

## CRIT-2: `/api/overlay` leaks on every cache miss

### What breaks

`api_overlay()` at line 1038–1086 never calls `conn.close()` **on any
path** — not in success, not in failure. The cursor is in a `with`
block (line 1043) but the connection is not.

```python
@app.route("/api/overlay")
@cache.cached(timeout=600)
def api_overlay():
    conn = get_db()
    with conn.cursor() as cur:
        # ... 3 queries ...
    return jsonify({...})    # conn.close() NEVER called
```

### Trigger

Literally every cache miss. The `@cache.cached(timeout=600)` means at
least one miss every 10 min per worker. With 2 workers, that's
12 leaks/hour **minimum**.

### Blast radius

Pool (8 slots) exhausted in ~40 min of steady traffic. Pool's
`max_lifetime=3600` and `max_idle=300` mean orphaned connections may
eventually expire, but by then the damage is done. This is a candidate
root cause for the 3rd wedge incident — the `/api/overlay` endpoint was
added in v0.12 (2026-04-15), and all three wedges happened after that.

### Fix

Add `try/finally`:

```python
conn = get_db()
try:
    with conn.cursor() as cur:
        # ... queries ...
    return jsonify({...})
finally:
    conn.close()
```

**High priority — will be fixed in the CRIT-1 batch.**

---

## CRIT-3: Unvalidated `float()` on user input

### What breaks

`/api/map` (line 1143) and `/api/heatmap` (line 1311) both do:

```python
south = request.args.get("south")   # returns string or None
north = request.args.get("north")
# ...
if all([south, north, west, east]):
    south_f = float(south)   # ValueError if not numeric
    north_f = float(north)
```

No `try/except`. No validation. A request like:

```
GET /api/map?south=x&north=y&west=z&east=w
```

hits `all([...])` = True (all non-empty), then `float("x")` raises
`ValueError` → unhandled exception → Flask returns 500.

**Because `conn = get_db()` at line 1126 runs BEFORE the `float()`
calls at line 1143**, this bad input **leaks a connection** every
time (combined with CRIT-1).

### Trigger

Any malformed URL. A scanner/crawler hitting `/api/map?south=foo`
eight times — or a single bot with buggy URL encoding — wedges the
pool.

### Blast radius

8 malformed requests = pool dead. Same recovery as other wedges.
Especially bad because this is a **remote attack vector**: anyone can
DoS the site by sending a few dozen malformed requests.

### Fix

Helper for parsing + 400 response:

```python
def _parse_float(value, name):
    try:
        return float(value)
    except (ValueError, TypeError):
        raise BadRequest(f"{name} must be a number, got {value!r}")
```

Then in routes:

```python
south_f = _parse_float(south, "south")
```

Flask's `BadRequest` → HTTP 400 with a JSON body. User sees a clear
error, pool is unaffected.

---

## HIGH-4: Stale prewarm lockfile after worker crash

### What breaks

The prewarm coordination at line 3308+ uses `/tmp/ufosint_prewarm.lock`
created with `O_EXCL | O_CREAT`. Exactly one worker becomes leader;
others are followers that wait for the lockfile to disappear.

**If the leader worker crashes** (OOM-kill, SIGKILL, Azure container
termination mid-prewarm) the lockfile is **never released**. On the
next worker restart:

1. Both workers try `O_EXCL | O_CREAT` → both get `FileExistsError`
2. Both become followers
3. Both wait up to `FOLLOWER_WAIT_SECS = 180` seconds
4. Both eventually run fallback `_run_leader_warm()` in parallel
5. → Back to the contention bug the lock was designed to fix
6. Prewarm takes 2–3x longer, first user requests hit cold caches

The `/tmp` directory persists across worker restarts within the same
container. It does NOT persist across full container restarts (Azure
slot swap, scale events).

### Trigger

- gunicorn `--max-requests` causes worker recycle mid-prewarm (we
  don't set this, but could)
- Azure infrastructure issue kills the leader
- OOM kill during the big `/api/points-bulk` scan
- Deploy mid-prewarm (unlikely but possible)

### Blast radius

One bad deploy/restart → 3 minutes of slow startup → users see
cold-cache response times (10–30s) until the fallback prewarm
completes. Not a full outage but a major UX regression.

### Fix

Check the lockfile's mtime. If >90s old, treat as stale and take
over:

```python
def _acquire_leader_lock():
    try:
        fd = os.open(PREWARM_LOCK_PATH, os.O_EXCL | os.O_CREAT | os.O_WRONLY, 0o644)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)
        return True
    except FileExistsError:
        # Check if the holder is still alive via lockfile mtime.
        try:
            age = time.time() - os.path.getmtime(PREWARM_LOCK_PATH)
            if age > 90:  # older than reasonable prewarm runtime
                os.unlink(PREWARM_LOCK_PATH)
                return _acquire_leader_lock()  # retry
        except FileNotFoundError:
            return _acquire_leader_lock()  # race: try again
        return False
```

Or switch to `fcntl.flock()` which is released by the kernel when the
holder dies. Cleaner, no stale-file problem.

---

## HIGH-5: `_points_bulk_build` lock has no timeout

### What breaks

Line 1883:

```python
def _points_bulk_build(etag: str) -> tuple[bytes, bytes, dict]:
    lock = _get_points_bulk_lock(etag)
    with lock:                              # ← no timeout
        return _points_bulk_build_cached(etag)
```

If thread A enters `_points_bulk_build_cached()` and hangs (DB hiccup,
memory pressure, network blip during the 396k-row scan), threads B, C,
D, ... all block on `with lock:` **indefinitely** (or until gunicorn's
180s worker timeout SIGKILLs them).

### Trigger

- PG query hangs (network partition, PG memory pressure)
- Thread A's `get_db()` succeeds but the big SELECT stalls
- Any exception in the build that doesn't release cleanly

### Blast radius

Every concurrent request for `/api/points-bulk` blocks until 180s
worker timeout. With 2 workers × 4 threads = 8 slots, a single
wedged thread can starve 7 other requests. Combined with `/api/
points-bulk` being the **first call** on every Observatory page load,
this is a loud failure mode.

### Fix

Use `lock.acquire(timeout=30)`:

```python
def _points_bulk_build(etag: str):
    lock = _get_points_bulk_lock(etag)
    acquired = lock.acquire(timeout=30)
    if not acquired:
        # Fall back to uncoordinated build — worse for contention,
        # but better than blocking the worker indefinitely.
        return _points_bulk_build_cached(etag)
    try:
        return _points_bulk_build_cached(etag)
    finally:
        lock.release()
```

---

## HIGH-6: gunicorn timeout (180s) ≫ pool timeout (8s)

### What breaks

`Procfile`:
```
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 180
```

Pool config: `timeout=8`, `statement_timeout=25000` (25s).

The math doesn't line up:
- Pool says "give up after 8s" → 503
- Statement says "kill query after 25s" → error
- gunicorn says "kill worker after 180s" → SIGKILL

**A request can hang for 180s before gunicorn notices.** If the worker
thread is stuck in a bad place (locked without timeout, as in HIGH-5,
or an infinite loop in payload building), gunicorn only rescues it
after 3 minutes. During those 3 minutes, the worker slot is useless
but Azure Health Check's `/health` probes may still succeed via
other slots, masking the outage.

### Trigger

Any of:
- Bug causes infinite loop in payload building
- `_points_bulk_build` lock wedge (HIGH-5)
- Large response streaming stalls mid-write
- Thundering herd of slow clients holding connections open

### Blast radius

Long detection windows. Users wait 3 minutes per request instead of
getting a fast 503. Azure's `HealthCheckStatus` metric may lie about
instance health because `/health` responses use a separate thread that
may still be healthy.

### Fix

Reduce gunicorn `--timeout` to match realistic request time:

```
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 \
    --timeout 35 --graceful-timeout 30
```

35s is pool (8s) + statement (25s) + 2s margin. Any request that
exceeds this is definitely stuck. `/api/points-bulk` worst case is
~25s cold build; 35s leaves headroom without being generous.

Also consider `--max-requests 10000 --max-requests-jitter 500` to
proactively recycle workers — catches slow memory leaks and keeps
them fresh.

---

## MED-7: No rate limiting — `/api/map?limit=100000` is a DoS amplifier

### What breaks

`/api/map` accepts user-controlled `limit` up to 100,000. Each marker
has multiple fields (lat, lng, id, shape, date, ...). A 100k-marker
response is ~10 MB JSON.

**A single request:**
- Holds a pool slot for ~20s (large scan)
- Runs a COUNT(*) on the same 8M-row table (~5s more)
- Returns 10 MB uncompressed (1.5 MB gzipped)
- Consumes ~50 MB of worker RAM to build the response

**Eight concurrent requests** from one IP would exhaust the pool AND
the worker memory. No rate limit exists. No IP throttling. No WAF.

### Trigger

- Malicious actor
- Badly-written scraper
- Someone reshared a link that triggers a big query

### Blast radius

Self-DoS. Pool exhausted until the 8 heavy queries complete (minutes).
Memory pressure could trigger OOM kill on B1 (1.75 GB total).

### Fix

1. Add Flask-Limiter (~2h effort):
   ```python
   from flask_limiter import Limiter
   limiter = Limiter(app, key_func=get_remote_address,
                     default_limits=["100 per minute"])

   @app.route("/api/map")
   @limiter.limit("30 per minute")
   def api_map(): ...
   ```
2. Cap `limit` harder for anonymous requests:
   ```python
   req_limit = min(int(req_limit), 100000)  # current
   # → 
   req_limit = min(int(req_limit), 25000)   # new default cap
   # Only allow >25k for authenticated / API-key requests
   ```
3. Enable Azure Front Door WAF (~$35/mo) with rate-limit rules. Catches
   attacks before they hit the App Service.

---

## MED-8: FILTER_CACHE never refreshed after startup

### What breaks

Line 330: `FILTER_CACHE = {}` populated once by `init_filters()` at
startup. Never refreshed.

After a data reload (ufo-dedup pipeline runs + new shapes/sources/
emotions appear), the cache is **stale** until the worker restarts.
With `alwaysOn=true`, workers may live for days or weeks.

### Trigger

- Run the ETL pipeline in ufo-dedup
- Data import adds new shape values
- User picks the new shape from... nowhere, because it's not in the
  dropdown

### Blast radius

Silent bug. Users can't filter by newly-ingested values. Not a crash,
but a UX trap where "why doesn't 'Tic-tac' appear in the dropdown?" —
answer: worker was started before the data landed.

### Fix

Either:
1. Refresh on a TTL (e.g. every 10 min):
   ```python
   _FILTER_CACHE_EXPIRES = 0
   def get_filters():
       global _FILTER_CACHE_EXPIRES
       if time.time() > _FILTER_CACHE_EXPIRES:
           init_filters()
           _FILTER_CACHE_EXPIRES = time.time() + 600
       return FILTER_CACHE
   ```
2. Or rely on the materialized view MV that ufo-dedup refreshes — make
   `init_filters()` read from there, then rebuild the MV post-ETL.

---

## MED-9: Pool size = concurrent slots, no safety margin for prewarm

### What breaks

- Pool `max_size=8`
- gunicorn 2 × 4 = 8 concurrent request slots
- Prewarm thread runs in the same worker as real requests and calls
  `app.test_client().get(...)` which hits real routes that call
  `get_db()` — these also take pool slots

**During the 20–40 s prewarm window, the prewarm thread can hold 1–2
pool connections**. If a real user lands during prewarm, they compete
for the remaining 6–7 slots. Most of the time this is fine, but a
flash crowd landing at exactly the right moment gets `PoolTimeout`.

### Trigger

- Cold start coinciding with a traffic spike
- Post-deploy (container just restarted) + a Reddit link hits hot

### Blast radius

Minor, transient. A few users get 503 during the prewarm window.
Resolves in seconds as prewarm completes.

### Fix

Bump pool to `max_size=10` or `max_size=12`. PG B1ms has ~50
max_connections, and our real concurrent need is 8, so we have room.
Cost: ~$0 (PG isn't connection-bound at this scale).

```python
_pool = ConnectionPool(
    DATABASE_URL,
    min_size=2,      # was 1 — keep 2 warm for fast first-request
    max_size=12,     # was 8 — headroom for prewarm thread
    # ... rest unchanged ...
)
```

---

## MED-10: No client-side error state when API returns 5xx

### What breaks

When `/api/*` returns 500/503, the frontend code silently fails. The
map renders with tiles but zero markers. The timeline is empty. The
stats badge shows blank.

**Users see a broken-looking page with no explanation.** They assume
it's their network, blame the site, or just leave.

### Trigger

Any API outage (historical — three times in 24 h).

### Blast radius

User confusion. Support load. Reputation.

### Fix

Add a global fetch wrapper that catches 5xx and shows a dismissable
banner:

```javascript
async function safeFetch(url, opts) {
  const resp = await fetch(url, opts);
  if (!resp.ok) {
    if (resp.status >= 500) {
      showErrorBanner(
        "ufosint is having trouble right now. " +
        "Try again in a minute — we've been notified."
      );
    }
    throw new Error(`HTTP ${resp.status}`);
  }
  return resp;
}
```

Wire it into every API call in `static/app.js`. ~2–3 hours of work.

---

## LOW-11: Shared PG between staging and prod = coupled blast radius

### What breaks

From CLAUDE.md and OPERATIONS.md:

> Staging App Service points at the same Postgres as prod.

If the staging app has a bug that runs a heavy query, it hits the
prod PG. If someone tests a new migration on staging, prod sees it
too. If staging spins into a request loop, prod's pool competes for
PG's 50 max_connections.

### Trigger

- Staging deploy with a buggy migration
- Staging load test
- Dev accidentally pointing local env at production DB

### Blast radius

Cross-contamination. Not a likely failure mode since we're disciplined
about staging, but it means staging incidents can become prod
incidents. Also a security/audit smell: prod DB credentials exist in
two places.

### Fix

Long-term: separate PG for staging. Cheaper option: keep shared PG but
create a `readonly_staging` role on PG with restrictive permissions
and use it for the staging DATABASE_URL. Prevents staging from writing
to prod tables even by accident.

---

## Summary Recommendations

### Ship today (< 1 hour total)

- [ ] **CRIT-1**: Wrap all 10 unsafe routes in `try/finally`
- [ ] **CRIT-2**: Fix `/api/overlay` connection leak
- [ ] **CRIT-3**: Validate `float()` input, return 400 on bad data
- [ ] **HIGH-6**: Drop gunicorn timeout 180 → 35
- [ ] **MED-9**: Bump pool max_size 8 → 12

### This week (< 1 day)

- [ ] **HIGH-4**: Stale-lockfile takeover logic
- [ ] **HIGH-5**: Timeout on `_points_bulk_build` lock
- [ ] **MED-8**: TTL on FILTER_CACHE

### Next sprint

- [ ] **MED-7**: Rate limiting (Flask-Limiter or Azure Front Door)
- [ ] **MED-10**: Client-side error banner
- [ ] **LOW-11**: Separate PG user for staging

---

*Audit performed 2026-04-17 by Claude Opus 4.6 (1M context).*
*Next review: after next production incident, or 2026-07-17,
whichever comes first.*
