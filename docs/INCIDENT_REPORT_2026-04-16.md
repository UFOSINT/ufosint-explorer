# Incident Report: Production Connection Pool Death Spiral

**Service:** ufosint.com (ufosint-explorer)
**Date range:** 2026-04-16 08:00 UTC — 2026-04-17 02:30 UTC (~18.5 h incident window)
**Report date:** 2026-04-17
**Severity:** SEV-1 (complete API outage, multiple recurrences)
**Author:** Claude Opus 4.6 (1M context) / automated analysis

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Timeline of Events](#2-timeline-of-events)
3. [Impact Assessment](#3-impact-assessment)
4. [Root Cause Analysis](#4-root-cause-analysis)
5. [The Death Spiral Mechanism](#5-the-death-spiral-mechanism)
6. [Why Each Fix Was Insufficient](#6-why-each-fix-was-insufficient)
7. [Final Resolution](#7-final-resolution)
8. [Azure Metrics Deep Dive](#8-azure-metrics-deep-dive)
9. [Detection Gap Analysis](#9-detection-gap-analysis)
10. [Infrastructure Review](#10-infrastructure-review)
11. [Defense Stack (Current State)](#11-defense-stack-current-state)
12. [Recommendations](#12-recommendations)
13. [Appendices](#13-appendices)

---

## 1. Executive Summary

Over an 18.5-hour window spanning April 16–17 2026, the ufosint.com
production site experienced **three separate complete API outages**
caused by the same underlying mechanism: PostgreSQL connection pool
exhaustion due to silently-dropped TCP connections.

Each outage rendered all `/api/*` endpoints non-functional (HTTP 500),
while the HTML shell and static assets continued to serve normally
(HTTP 200). Users could load the page but saw no data — no map points,
no stats, no timeline, no overlays.

The site was **not overwhelmed by traffic**. The third outage occurred
with literally one visitor (a Reddit user on Android). The problem was
entirely infrastructural: a combination of Azure App Service container
idling (`alwaysOn: false`), Azure's network address translation (NAT)
silently dropping idle TCP connections, and Linux's default 2-hour TCP
keepalive timeout preventing the application from detecting the dead
sockets.

Three hotfix versions were shipped in rapid succession:

| Version | Fix | Sufficient? |
|---------|-----|-------------|
| v0.12.1 | `check=SELECT 1` before handout + `max_idle` + `max_lifetime` | No — `SELECT 1` hangs on silently-dropped sockets |
| v0.12.2 | `connect_timeout=5` + `statement_timeout=25000` + pool `timeout=8` + `/health` returns 503 | No — timeouts only help fresh connections; `check` still hangs on existing dead FDs |
| v0.12.3 | TCP keepalive (`keepalives_idle=60`) + `alwaysOn=true` | **Yes** — OS detects dead sockets in ~110s; container stays warm |

**Total estimated downtime:** ~12 hours across 3 incidents.
**Users impacted:** All visitors during outage windows. Traffic data
shows 100–800 requests/hour during affected periods, meaning hundreds
of visitors experienced failures.

---

## 2. Timeline of Events

All times UTC. The incident window spans two calendar days.

### Pre-incident context

- **2026-04-15 ~04:00.** v0.12.0 (UAP Gerb overlay) deployed to prod.
  Deploy smoke tests pass. Site is healthy.
- **2026-04-15 entire day.** Normal operations. Traffic: 79–10,842
  requests/hour. Avg response time: 0.01–0.84 s. Zero 5xx errors
  (except 1 at 13:00, likely a transient).
- **2026-04-15 ~21:00.** Some burst activity (494 requests, 94.9s
  CPU time). Still healthy.

### Incident 1: The First Wedge

| Time | Event |
|------|-------|
| 04-16 05:00–07:00 | Traffic drops to 43–79 req/h. Container enters low-activity period. |
| 04-16 ~07:30 | Azure NAT silently drops idle PG connections. Pool sockets become half-dead (client-side open, server-side closed). No mechanism detects this. |
| 04-16 08:00 | **Wedge begins.** First requests of the morning hit the pool. `getconn()` hands out zombie sockets. Queries hang 30 s. `PoolTimeout` raised. **16 5xx errors this hour.** Avg response time spikes to **7.25 s**. |
| 04-16 09:00 | **23 5xx errors.** Avg response time: **8.6 s**. Pool is completely saturated with dead connections. Every API request waits the full 30s timeout then fails. |
| 04-16 10:00 | **54 5xx errors** (worst single hour). Avg response time: **18.96 s**. Only 46 of 241 requests succeed (19% success rate). |
| 04-16 11:00 | **30 5xx errors.** 45 of 134 requests succeed (34%). |
| 04-16 12:00 | **25 5xx errors.** 40 of 140 requests succeed (29%). |
| 04-16 13:00 | **29 5xx errors.** User reports site is stuck. Manual investigation begins. |
| 04-16 13:12 | User provides console screenshots showing all `/api/*` returning 500. |
| 04-16 13:13 | `az webapp restart` issued. Pool reinitialized with fresh connections. |
| 04-16 13:14 | `/health` returns 200 with live counts. All endpoints verified green. |
| 04-16 ~13:30 | **v0.12.1 developed and shipped.** Adds `check=ConnectionPool.check_connection`, `max_idle=300`, `max_lifetime=3600` to pool config. |
| 04-16 14:00 | Site healthy. 0 5xx errors. Avg response time: 0.5 s. |

**Incident 1 duration: ~6 hours** (08:00–13:13 UTC)
**Total 5xx errors in Incident 1: 177**
**Detection method: User report (manual)**

### Incident 2: The Second Wedge

| Time | Event |
|------|-------|
| 04-16 14:00–16:00 | v0.13 UX polish work deployed to staging, then to prod. Multiple deploys. Traffic: 79–185 req/h. Healthy. |
| 04-16 17:00 | **13 5xx errors** begin appearing. Avg response time: 2.4 s. Pool starting to degrade. |
| 04-16 18:00 | **114 5xx errors** (second-worst hour). Avg response time: **16.1 s**. Only 136 of 411 requests succeed (33% success rate). Pool completely wedged again. |
| 04-16 ~18:24 | User reports site is down again. Logs show `PoolTimeout: couldn't get a connection after 30.00 sec`. |
| 04-16 ~18:25 | `az webapp restart` issued. |
| 04-16 ~18:30 | Site restored. Investigation reveals that `check=SELECT 1` hangs on silently-dropped sockets because the OS doesn't know the TCP connection is dead. |
| 04-16 ~19:00 | **v0.12.2 developed, tested, and shipped.** Adds `connect_timeout=5`, `statement_timeout=25000`, pool `timeout=30→8`, `/health` returns 503 on failure. Azure Health Check enabled at `/health`. |
| 04-16 19:00 | Site healthy. 0 5xx errors. 641 successful requests. Avg response time: 0.11 s. |

**Incident 2 duration: ~2 hours** (17:00–~18:30 UTC)
**Total 5xx errors in Incident 2: 127**
**Detection method: User report (manual)**

### Incident 3: The Overnight Death Spiral

| Time | Event |
|------|-------|
| 04-16 19:00–21:00 | Healthy. Health Check: 100%. Traffic: 658–712 req/h. |
| 04-16 21:00 | **12 5xx errors.** Pool starting to degrade. |
| 04-16 22:00 | **55 5xx errors.** Health Check drops to **93%**. Pool is failing intermittently. Avg response time: 0.74 s (fast failures thanks to v0.12.2's 8s timeout). |
| 04-16 22:00–22:30 | `alwaysOn=false` lets Azure begin idling the container. Traffic drops. |
| 04-16 23:00 | **Health Check: 0%.** Site is completely dead. 32 5xx errors out of 527 total requests (health check probes + any real visitors). **467 requests succeed** — these are likely static asset requests that don't need the DB. |
| 04-17 00:00 | **Health Check: 0%.** 94 5xx errors. 474 successful requests (static assets). |
| 04-17 01:00 | **Health Check: 0%.** 37 5xx errors. Single Reddit user arrives (referrer: `android-app://com.reddit.frontpage/`). Their page loads (HTML 200) but all API calls fail with `PoolTimeout: couldn't get a connection after 8.00 sec`. |
| 04-17 01:46 | Log shows: `"GET /health HTTP/1.1" 503` — HealthCheck/1.0. The v0.12.2 fix is correctly reporting 503 to Azure. |
| 04-17 01:47 | Log shows Reddit user's requests: `GET /` → 200, `GET /api/overlay` → 500 (`PoolTimeout`), `GET /api/timeline` → 500. |
| 04-17 ~02:00 | User discovers the outage and reports to us. |
| 04-17 ~02:05 | `az webapp restart` issued. Investigation reveals `alwaysOn: false` — the smoking gun. |
| 04-17 02:00 | Health Check recovers to **28%** (restart mid-hour). |
| 04-17 ~02:15 | `alwaysOn=true` enabled via `az webapp config set`. |
| 04-17 ~02:30 | **v0.12.3 developed, tested, and shipped.** Adds TCP keepalive parameters (`keepalives=1`, `keepalives_idle=60`, `keepalives_interval=10`, `keepalives_count=5`). |

**Incident 3 duration: ~4 hours** (22:00–~02:05 UTC)
**Total 5xx errors in Incident 3: ~218**
**Detection method: User report (manual) — Azure Health Check detected it at 22:00 but had no mechanism to alert a human**

### Post-incident

| Time | Event |
|------|-------|
| 04-17 02:30 | All endpoints verified 200. Response times: 0.09–0.23 s. |
| 04-17 02:30–present | Monitoring via 15-min cron probe in Claude session. |

---

## 3. Impact Assessment

### Downtime Summary

| Incident | Start | End | Duration | 5xx Errors | Detection |
|----------|-------|-----|----------|------------|-----------|
| #1 | 04-16 08:00 | 04-16 13:13 | **5h 13m** | 177 | Manual (user report after 5h) |
| #2 | 04-16 17:00 | 04-16 18:30 | **1h 30m** | 127 | Manual (user report after ~1h) |
| #3 | 04-16 22:00 | 04-17 02:05 | **4h 05m** | ~218 | Manual (user report after ~4h) |
| **Total** | | | **~10h 48m** | **~522** | |

### Traffic During Outages

Azure metrics show the site was receiving real traffic during every
outage window:

- **Incident 1 (08:00–13:00):** 76–241 requests/hour. Peak at 10:00
  with 241 requests, of which 54 (22%) returned 5xx.
- **Incident 2 (17:00–18:00):** 223–411 requests/hour. Peak at 18:00
  with 411 requests, of which 114 (28%) returned 5xx.
- **Incident 3 (22:00–01:00):** 527–793 requests/hour. These were
  the **busiest hours of the entire 48-hour window** — and the site
  was dead for all of them.

### What Users Experienced

1. **Page loads normally** — HTML shell and all static assets (CSS, JS,
   images) serve from the filesystem, no DB required. HTTP 200.
2. **Map is empty** — `/api/map` and `/api/points-bulk` return 500.
   The deck.gl/Leaflet map renders with tiles but zero sighting points.
3. **Stats show nothing** — `/api/stats` returns 500. The header badge
   shows "0 sightings" or an error state.
4. **Timeline is blank** — `/api/timeline` returns 500. The TimeBrush
   histogram has no data.
5. **Overlays fail silently** — `/api/overlay` returns 500. Crash,
   Nuclear, and Facility markers don't appear.
6. **No error message shown to user** — The frontend does not display
   a "service unavailable" banner when API calls fail. Users see a
   page that looks broken with no explanation.

### User Experience Gap (Action Item)

The app has no client-side error state for API failures. When the
backend is down, the user sees an empty map with no indication that
something is wrong. This should be addressed in a future version.

---

## 4. Root Cause Analysis

### The Fundamental Problem

The root cause is a **mismatch between four independent timeout/
lifecycle assumptions**:

| Component | Assumes connections live for... | Actually... |
|-----------|-------------------------------|-------------|
| **Azure NAT gateway** | Drops idle TCP after **~4 min** | No notification to either end |
| **Linux kernel** | TCP keepalive default: **~2 hours** | Won't probe until way past Azure's drop |
| **psycopg_pool** | Trusts the OS's view of socket state | Hands out sockets OS says are open |
| **App Service (alwaysOn=false)** | Container can sleep after **~20 min** | All connections die during sleep |

When these four systems interact:

1. Azure NAT drops the TCP connection after a few minutes of idle
2. The Linux kernel doesn't know (it hasn't probed yet — 2h default)
3. psycopg_pool's `check=SELECT 1` tries to use the socket
4. The kernel dutifully sends the SELECT 1 packet into the dead NAT path
5. The packet is silently dropped (no RST, no ICMP unreachable)
6. The kernel retransmits, backs off, retransmits again... for up to
   the full TCP retransmit timeout (~2 hours on some Linux configs)
7. Meanwhile, the `check` callback is blocked, the pool slot is busy,
   and the request is stalled

Multiply this by 8 pool slots and you have a complete outage from a
single quiet period.

### Why Azure NAT Drops Connections Silently

Azure's load balancer and NAT gateway use connection tracking tables
with finite timeouts. The default idle timeout for TCP is **4 minutes**
(configurable up to 30 min on some SKUs). When a tracked connection
goes idle past this timeout:

1. Azure removes the tracking entry from the NAT table
2. Neither the client nor the server is notified
3. Any packet sent after this point is **silently dropped** — Azure
   doesn't know where to route it because the NAT mapping is gone
4. No RST is sent (Azure doesn't have the NAT state to forge one)
5. No ICMP unreachable is sent (Azure's NAT gateway doesn't do this)

This is a well-known characteristic of cloud NAT gateways. AWS, GCP,
and Azure all exhibit this behavior. The standard mitigation is TCP
keepalive with an interval shorter than the NAT timeout.

### Why `alwaysOn=false` Made It Catastrophic

Without Always On:

1. After ~20 min of no HTTP requests, Azure deallocates the container
2. The gunicorn process terminates (all 8 pool connections abandoned)
3. On the next request, Azure cold-starts a new container
4. gunicorn starts, psycopg_pool initializes with `min_size=1`
5. The single initial connection works (fresh TCP handshake)
6. Subsequent requests open more connections (up to `max_size=8`)
7. As traffic subsides, connections go idle
8. Azure NAT drops them after ~4 min
9. Next request → `check` hangs → PoolTimeout → 500

With Always On, the container stays warm and connections stay active
(or at least the TCP keepalive probes keep them "warm" in Azure's NAT
table).

---

## 5. The Death Spiral Mechanism

The death spiral has a specific, reproducible sequence:

```
┌─────────────────────────────────────────────────────────┐
│                    NORMAL OPERATION                      │
│                                                         │
│  App Service ──TCP──> Azure NAT ──TCP──> PostgreSQL     │
│  (8 pool slots)       (tracking)        (Burstable B1ms)│
│                                                         │
│  Requests flow normally. Pool hands out connections.    │
│  check=SELECT 1 passes instantly (socket is alive).     │
└─────────────────────────────────┬───────────────────────┘
                                  │
                          traffic stops
                          (idle > 4 min)
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────┐
│                    SILENT DISCONNECT                     │
│                                                         │
│  App Service ──TCP──>    ???    ──────> PostgreSQL       │
│  (sockets "open")   (NAT mapping       (connections     │
│                      REMOVED)           closed server-   │
│                                         side, or just    │
│  Linux kernel:                          orphaned)        │
│  "These sockets                                         │
│   look fine to me"                                      │
│  (no keepalive                                          │
│   probe for 2h)                                         │
└─────────────────────────────────┬───────────────────────┘
                                  │
                          new request arrives
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────┐
│                    WEDGE CASCADE                         │
│                                                         │
│  1. getconn() picks a connection from the pool          │
│  2. check=SELECT 1 runs on the connection               │
│  3. Kernel sends packet → Azure drops it (no NAT entry) │
│  4. No RST, no error — kernel waits and retransmits     │
│  5. check() blocks for 15s–2h (TCP retransmit timeout)  │
│  6. Pool slot is stuck. Can't be reused or replaced.    │
│  7. Next request picks another dead connection → repeat  │
│  8. All 8 slots stuck → PoolTimeout for everyone        │
│                                                         │
│  /health also calls getconn() → also hangs → also 503   │
│                                                         │
│  Azure Health Check sees 503 but can't help on B1       │
│  (single instance, no sibling to route to)              │
└─────────────────────────────────────────────────────────┘
```

### Why It Self-Perpetuates

Once the spiral starts, it doesn't self-resolve because:

1. **Dead slots don't free up.** The `check` callback is blocking on
   a syscall (send/recv on a dead socket). The pool can't time out
   the check — there's no `check_timeout` parameter in psycopg_pool.
2. **New connections can't be opened.** All 8 slots are occupied by
   the stuck `check` calls. Even if `connect_timeout=5` would let a
   fresh connection succeed, there's no slot to put it in.
3. **The pool timeout fires before the check completes.** With
   `timeout=8`, the request gives up and returns 503 after 8 seconds.
   But the stuck `check` calls continue occupying their slots.
4. **Eventually the kernel gives up** on the dead sockets (after the
   TCP retransmit timeout — 15–120s depending on configuration). The
   `check` callback finally gets an error, the pool discards the
   connection and opens a new one. But this takes minutes per slot,
   and during that time the pool is at reduced capacity.

Without TCP keepalive, recovery requires either:
- Waiting for all 8 kernel-level TCP retransmit timeouts to fire
  (minutes to hours)
- Manual `az webapp restart` (kills the process, fresh pool)
- Azure Health Check auto-restart (10-min threshold, but on B1
  single-instance the behavior is restart-in-place, which takes
  another 30–60s cold start)

---

## 6. Why Each Fix Was Insufficient

### v0.12.1: `check=ConnectionPool.check_connection`

**What it does:** Before handing out a connection, run `SELECT 1` on
it. If the query fails, discard the connection and open a new one.

**Why it wasn't enough:** `SELECT 1` is a normal SQL query that goes
through the kernel's TCP stack. If the kernel thinks the socket is
alive (because it hasn't probed yet — 2h default keepalive), it
sends the packet into the dead NAT path. The packet is silently
dropped. The kernel retransmits with exponential backoff. The `check`
callback blocks for 15s–2h waiting for the retransmit to either
succeed or time out.

**In other words:** `check` only works if the kernel already knows
the socket is dead. Without TCP keepalive, the kernel is clueless.

### v0.12.2: `connect_timeout=5` + `statement_timeout=25000` + `timeout=8`

**What it does:**
- `connect_timeout=5`: Opening a *new* connection times out after 5s
- `statement_timeout=25000`: PG kills any query running >25s
- `timeout=8`: Pool gives up handing out a connection after 8s
- `/health` returns 503 on failure (for Azure Health Check)

**Why it wasn't enough:**
- `connect_timeout` only applies to *new* connections (TCP SYN).
  The `check` callback runs on *existing* connections that are
  already "established" from the kernel's perspective.
- `statement_timeout` is a **server-side** setting. When the socket
  is dead at the NAT level, PG never receives the query. It can't
  kill what it never saw.
- `timeout=8` helps individual requests fail fast (503 in 8s instead
  of 30s), but it doesn't free up the stuck pool slots. The `check`
  callbacks continue blocking in the background.
- `/health` returning 503 helps Azure detect the outage, but on B1
  single-instance, Azure Health Check's only action is restart-in-
  place, which takes time and doesn't guarantee the restart won't
  immediately wedge again (if connections are still going through
  the same dead NAT path).

### v0.12.3: TCP keepalive + Always On

**What it does:**
- `keepalives=1`: Enable TCP keepalive on every PG connection
- `keepalives_idle=60`: Start sending keepalive probes after 60s idle
- `keepalives_interval=10`: Send a probe every 10s after that
- `keepalives_count=5`: Declare the connection dead after 5 failed probes
- `alwaysOn=true`: Container stays warm, no idle sleep

**Why this is the real fix:**

1. **TCP keepalive probes the connection before Azure's NAT drops it.**
   Azure NAT timeout is ~4 min. Our keepalive probes start at 60s
   and repeat every 10s. As long as keepalive probes are flowing,
   Azure sees activity and keeps the NAT mapping alive. The
   connection never goes stale in the first place.

2. **If a connection does die, the OS knows within ~110 seconds.**
   60s idle + 5 × 10s probes = 110s. After that, the kernel marks
   the socket as dead. When `check=SELECT 1` runs on a socket the
   kernel already knows is dead, it fails **instantly** with
   `ECONNRESET` or `ETIMEDOUT`. The pool discards it and opens a
   fresh connection. No hang.

3. **Always On prevents the container from sleeping.** The container
   stays warm, gunicorn stays running, pool connections stay active.
   No cold-start cycle that requires re-establishing all connections
   through a potentially stale NAT path.

**Together, these two changes break the death spiral at two points:**
- Always On prevents the idle period that triggers the NAT drop
- TCP keepalive provides a safety net if a connection does go idle:
  either the keepalive probe keeps the NAT mapping alive, or it
  detects the dead connection within 110s so `check` can replace it

---

## 7. Final Resolution

### Current Defense Stack (6 layers)

```
Layer 0: Always On
└── Container stays warm. No idle sleep. Connections stay active.
    First line of defense: prevent the problem from occurring.

Layer 1: TCP Keepalive (keepalives_idle=60)
└── OS probes idle sockets every 60s. If Azure NAT drops the
    connection, the OS detects it in ~110s and marks the socket
    as dead. Safety net for Layer 0.

Layer 2: Pool check (check=ConnectionPool.check_connection)
└── SELECT 1 before handing out a connection. If the OS already
    knows the socket is dead (thanks to Layer 1), check fails
    instantly. Pool discards the dead connection and opens a
    fresh one. Self-healing.

Layer 3: Idle/Lifetime recycling (max_idle=300, max_lifetime=3600)
└── Proactive connection turnover. Even if everything else works
    fine, connections are recycled every 5 min idle or 1 hour
    absolute. Prevents long-lived connections from accumulating
    drift.

Layer 4: Fail-fast timeouts (timeout=8, connect_timeout=5, statement_timeout=25s)
└── If a request can't get a connection in 8s, return 503 to the
    load balancer instead of holding the worker thread. Limits
    blast radius. connect_timeout caps fresh dials. statement_timeout
    is the server-side kill switch.

Layer 5: Azure Health Check (/health → 503 on failure)
└── Platform-level safety net. If the app is returning 503 for
    10+ minutes, Azure auto-restarts the instance. Last resort
    when all code-level defenses fail.
```

### Configuration (as shipped in v0.12.3)

```python
_pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=8,
    open=True,
    timeout=8,
    max_idle=300,
    max_lifetime=3600,
    check=ConnectionPool.check_connection,
    kwargs={
        "autocommit": True,
        "connect_timeout": 5,
        "keepalives": 1,
        "keepalives_idle": 60,
        "keepalives_interval": 10,
        "keepalives_count": 5,
        "options": (
            "-c default_transaction_read_only=on "
            "-c statement_timeout=25000"
        ),
    },
)
```

### Azure Configuration

```
alwaysOn: true
healthCheckPath: /health
```

---

## 8. Azure Metrics Deep Dive

### Health Check Status (% healthy per hour)

The Health Check was only enabled at ~18:30 on April 16 (v0.12.2).
Prior to that, no health check data exists.

```
Apr 16 19:00  ████████████████████████████████████████████████████  100%
Apr 16 20:00  ████████████████████████████████████████████████████  100%
Apr 16 21:00  ████████████████████████████████████████████████████  100%
Apr 16 22:00  ████████████████████████████████████████████████████   93%  ← degrading
Apr 16 23:00                                                          0%  ← DEAD
Apr 17 00:00                                                          0%  ← DEAD
Apr 17 01:00                                                          0%  ← DEAD
Apr 17 02:00  ██████████████                                         28%  ← restarted mid-hour
```

**Key insight:** Health Check detected the outage at 22:00 (93% →
dropping) and confirmed it at 23:00 (0%). But no human was notified.
The site was dead for **4 hours** with Azure fully aware of the problem
but no alerting configured.

### HTTP 5xx Errors Per Hour

```
                     Incident 1              Incident 2    Incident 3
                     ┌──────────┐            ┌───┐         ┌──────────┐
Apr 15 13:00    █  1                                                   
Apr 16 08:00  ████████████████  16                                     
Apr 16 09:00  ███████████████████████  23                              
Apr 16 10:00  ██████████████████████████████████████████████████████  54
Apr 16 11:00  ██████████████████████████████  30                       
Apr 16 12:00  █████████████████████████  25                            
Apr 16 13:00  █████████████████████████████  29                        
Apr 16 14:00    0  ← restarted                                        
Apr 16 17:00  █████████████  13                                        
Apr 16 18:00  ██████████████████████████████████████████████████████████████████████████████████████████████████████████████████  114
Apr 16 19:00    0  ← restarted + v0.12.2                               
Apr 16 21:00  ████████████  12                                         
Apr 16 22:00  ███████████████████████████████████████████████████████  55
Apr 16 23:00  ████████████████████████████████  32                     
Apr 17 00:00  ██████████████████████████████████████████████████████████████████████████████████████████████  94
Apr 17 01:00  █████████████████████████████████████  37                
Apr 17 02:00  ██████████████████████████  26                           
```

**Total 5xx errors in 48-hour window: 541**

### Average Response Time Per Hour

Normal response time is 0.1–0.5 seconds. During outages, the average
spikes because failed requests hang for the full pool timeout (30s in
v0.12.1, 8s after v0.12.2).

```
Apr 16 08:00    7.26 s   ← Incident 1 begins
Apr 16 09:00    8.60 s
Apr 16 10:00   18.96 s   ← WORST HOUR (30s timeout, many requests)
Apr 16 11:00    9.85 s
Apr 16 12:00    9.65 s
Apr 16 13:00    8.66 s
Apr 16 14:00    0.51 s   ← restarted
Apr 16 17:00    2.38 s   ← Incident 2 begins
Apr 16 18:00   16.12 s   ← 30s timeout, max pain
Apr 16 19:00    0.11 s   ← restarted + v0.12.2 (8s timeout)
Apr 16 22:00    0.74 s   ← Incident 3 (8s timeout = faster 503s)
Apr 17 00:00    1.43 s
```

**Note the improvement from v0.12.2:** Incident 3's avg response time
peaked at 1.43s vs Incident 1's 18.96s, because the pool timeout was
reduced from 30s to 8s. Requests fail faster, which is better UX than
hanging for 30s.

### Request Volume Per Hour

```
Apr 15 01:00  10,638   ← Bot/crawl spike
Apr 15 03:00  10,842   ← Bot/crawl spike
Apr 15 04:00  10,285   ← Bot/crawl spike
Apr 15 05:00      79   ← Normal human traffic begins
  ... (100-500 range typical)
Apr 16 19:00     658   ← Post-v0.12.2 recovery, traffic rising
Apr 16 20:00     672   ← Evening traffic
Apr 16 21:00     712   ← Peak evening
Apr 16 22:00     793   ← HIGHEST hour — and the site was dying
Apr 17 00:00     609   ← Still busy, still dead
```

**Key insight:** The busiest traffic hours (19:00–22:00) coincided
with the onset of Incident 3. The site was getting the most visitors
it had seen all day, and they all got broken pages.

The early-morning bot spikes (01:00–04:00 on Apr 15) at 10k+ req/h
are crawler traffic. These are 20x higher than normal human traffic
but the site handled them fine — the bot traffic was never the problem.

### PostgreSQL Active Connections

```
Apr 15 00:00   10.6 avg
  ... (9-14 range typical — pool + staging + Azure internal)
Apr 16 08:00    9.4    ← Incident 1: fewer active (pool can't connect)
Apr 16 10:00    9.0    ← All app connections are dead zombie FDs
Apr 16 13:00   10.8    ← After restart, connections re-establish
Apr 16 18:00    6.3    ← Incident 2: pool connections dying
Apr 16 19:00   11.3    ← After restart + v0.12.2
Apr 16 22:00    9.7    ← Incident 3: connections dropping
Apr 17 01:00    8.7    ← Still degraded
Apr 17 02:00    9.7    ← After restart + v0.12.3
```

**PG was healthy the entire time.** CPU never exceeded 10.6%, active
connections stayed in the 6–14 range (well under the ~50 max for
B1ms). The problem was never on the PG side — it was always the
network path between App Service and PG.

### Memory Working Set

```
Apr 15: 600–665 MB (stable)
Apr 16 08:00–18:00: 585–647 MB (stable during incidents)
Apr 16 22:00: 740 MB (↑ 15% increase)
Apr 17 00:00: 757 MB (↑ still elevated)
Apr 17 01:00: 758 MB (↑ peak)
Apr 17 02:00: 662 MB (↓ after restart)
```

Memory crept up ~15% during Incident 3. This could be thread stacks
accumulating from stuck `check` callbacks, or Flask/gunicorn buffering
failed responses. The restart brought it back to baseline. Not a root
cause but worth noting — the 758 MB peak is getting close to B1's
1.75 GB limit.

---

## 9. Detection Gap Analysis

### How Long Were We Blind?

| Incident | Started | Detected | Blind time |
|----------|---------|----------|------------|
| #1 | 04-16 08:00 | 04-16 13:12 | **5h 12m** |
| #2 | 04-16 17:00 | 04-16 18:24 | **~1h 24m** |
| #3 | 04-16 22:00 | 04-17 02:00 | **~4h 00m** |

**Total blind time: ~10h 36m out of a ~10h 48m total outage.**

We were unaware of the problem for 98% of the outage duration.

### Why Azure Health Check Didn't Help Enough

1. **Health Check was only enabled at ~18:30 on April 16** (v0.12.2).
   Incidents 1 and 2 had no health check at all.

2. **B1 is single-instance.** Azure Health Check's primary mechanism
   is routing traffic to a healthy instance. With only one instance,
   there's nowhere to route. The fallback is auto-restart, but that
   has a long threshold (10 min LB, up to 1h for replacement) and
   on single-instance the behavior is restart-in-place.

3. **No alerting configured.** Azure Health Check can detect unhealthy
   status, but it doesn't email/SMS/webhook anyone. That requires a
   separate Azure Monitor Alert Rule.

### What We Need

1. **Azure Monitor Alert Rule** — triggers when `HealthCheckStatus`
   drops below 50% for 5+ minutes. Sends email/webhook immediately.
   This would have cut detection time from hours to minutes.

2. **External uptime monitoring** (e.g., UptimeRobot, Pingdom,
   Better Uptime) — an independent probe from outside Azure. If
   Azure itself has an issue, this catches it. Free tiers available.

3. **Client-side error reporting** — when the frontend gets 500s from
   the API, it should phone home (even just a pixel ping to a
   different endpoint or a third-party error tracker). This catches
   issues that server-side monitoring can't see.

---

## 10. Infrastructure Review

### Current Production Footprint

| Resource | Azure Name | SKU | Monthly Cost |
|----------|-----------|-----|-------------|
| App Service (prod) | ufosint-explorer | B2 Basic (1 instance) | ~$26 |
| App Service (staging) | ufosint-explorer-staging | B1 Basic (1 instance) | ~$13 |
| PostgreSQL | ufosint-pg | Burstable B1ms, 32 GB, PG 16 | ~$12 + storage |
| **Total** | | | **~$51/mo** |

### App Service Configuration (Post-Fix)

| Setting | Value | Notes |
|---------|-------|-------|
| SKU | B2 Basic | 1 core, 1.75 GB RAM |
| Instances | 1 | No redundancy |
| Always On | **true** (v0.12.3) | Was false before |
| Health Check | `/health` | Enabled in v0.12.2 |
| Python | 3.12 | Linux container |
| gunicorn | 2 workers × 4 threads | 8 concurrent request slots |

### PostgreSQL Configuration

| Setting | Value | Notes |
|---------|-------|-------|
| SKU | Burstable B1ms | 1 vCore, 2 GB RAM |
| Storage | 32 GB (~15% used) | Auto-grow disabled |
| Version | PostgreSQL 16 | |
| Max connections | ~50 | We use max 8 (pool) + staging |
| State during incidents | **Ready** | PG was never the problem |
| CPU during incidents | **6.6–10.6%** | Barely loaded |

### Risk: Single Point of Failure

The biggest architectural risk is that we run a **single App Service
instance**. This means:

- No redundancy — one stuck container = complete outage
- Azure Health Check can't route around the problem
- Auto-restart is the only recovery, and it takes 30–60s
- During the restart window, the site is down

Scaling to 2 instances on Standard tier (~$70/mo for S1 × 2) would
give Azure Health Check a healthy sibling to route to during a wedge.
But this doubles the App Service cost.

---

## 11. Defense Stack (Current State)

### Layer Diagram

```
┌────────────────────────────────────────────────────────────┐
│ Layer 0: Always On                                PREVENT  │
│ Container stays warm. Connections stay active.              │
│ Status: ✅ Enabled (v0.12.3)                               │
├────────────────────────────────────────────────────────────┤
│ Layer 1: TCP Keepalive (60s idle, 10s interval, 5 probes)  │
│ OS detects dead sockets in ~110s. Keeps NAT mapping alive. │ DETECT
│ Status: ✅ Shipped (v0.12.3)                               │
├────────────────────────────────────────────────────────────┤
│ Layer 2: Pool check (SELECT 1 before handout)              │
│ Dead connections replaced instantly (if OS knows they're    │ HEAL
│ dead — depends on Layer 1).                                │
│ Status: ✅ Shipped (v0.12.1)                               │
├────────────────────────────────────────────────────────────┤
│ Layer 3: Idle/Lifetime recycling (5 min / 1 hour)          │
│ Proactive connection turnover. Defense in depth.           │ RECYCLE
│ Status: ✅ Shipped (v0.12.1)                               │
├────────────────────────────────────────────────────────────┤
│ Layer 4: Fail-fast timeouts (8s pool, 5s connect, 25s PG)  │
│ Limits blast radius. Requests fail fast instead of hanging.│ CONTAIN
│ Status: ✅ Shipped (v0.12.2)                               │
├────────────────────────────────────────────────────────────┤
│ Layer 5: Azure Health Check (/health → 503 on failure)     │
│ Platform auto-restart after sustained failure.             │ RECOVER
│ Status: ✅ Enabled (v0.12.2)                               │
├────────────────────────────────────────────────────────────┤
│ Layer 6: Alerting                                          │
│ Notify human when site is unhealthy.                       │ ALERT
│ Status: ❌ NOT CONFIGURED                                  │
├────────────────────────────────────────────────────────────┤
│ Layer 7: Client-side error state                           │
│ Show users a "service unavailable" message.                │ UX
│ Status: ❌ NOT IMPLEMENTED                                 │
└────────────────────────────────────────────────────────────┘
```

---

## 12. Recommendations

### Immediate (do today)

| # | Action | Effort | Impact |
|---|--------|--------|--------|
| 1 | **Create Azure Monitor Alert Rule** on `HealthCheckStatus < 50%` for 5 min → email notification | 10 min | Cuts MTTR from hours to minutes |
| 2 | **Sign up for UptimeRobot** (free tier, 5-min checks) monitoring `https://ufosint.com/health` | 5 min | External probe independent of Azure |

### Short-term (this week)

| # | Action | Effort | Impact |
|---|--------|--------|--------|
| 3 | **Add client-side error banner** — when API calls return 5xx, show "Service temporarily unavailable" overlay | 1–2 hours | Users see a message instead of a broken page |
| 4 | **Verify fix overnight** — check Health Check metrics tomorrow morning | 5 min | Confidence |

### Medium-term (next sprint)

| # | Action | Effort | Impact |
|---|--------|--------|--------|
| 5 | **Scale to Standard S1 × 2 instances** | 30 min + ~$70/mo | True redundancy; Health Check can route around failures |
| 6 | **Add `/api/status` lightweight endpoint** that returns 200 without DB access | 30 min | Separates "is the app alive?" from "is the DB alive?" for more granular monitoring |
| 7 | **Rate limiting** (Flask-Limiter) on `/api/*` | 2–3 hours | Prevents any single client from exhausting pool slots (not the cause here, but defense in depth) |

### Long-term (backlog)

| # | Action | Effort | Impact |
|---|--------|--------|--------|
| 8 | **Azure Application Insights** integration | 2–4 hours | Full APM: request traces, dependency tracking, failure analysis |
| 9 | **Observability dashboard** in Azure portal | 1–2 hours | Pin 4 key metrics: request rate, 5xx rate, PG connections, response time |
| 10 | **PG connection pooler** (PgBouncer) between App Service and PG | 4–8 hours | Handles connection lifecycle at the proxy level; standard pattern for cloud PG |

---

## 13. Appendices

### A. Commit History (Incident Window)

```
4c791d3 v0.12.3 hotfix: TCP keepalive + Always On (3rd wedge fix)
2774917 v0.12.2 hotfix: fail-fast /health + pool timeouts
7f15931 v0.13 mobile polish 2: topbar wrap + overview visibility + viewport-fit
b63cc4e v0.13 mobile polish: hide site title + TimeBrush edge-safe padding
95a7d8e v0.13 Tier 1 UX polish: header collapse + 4 mobile/visual fixes
8345ad9 v0.13 UX polish backlog: 15 items across 3 tiers
18f9631 v0.12.1: Self-healing psycopg pool + ops runbook
6040d69 v0.12 docs: CHANGELOG, README overlays, /llms.txt, ARCHITECTURE + CLAUDE.md
e16ce08 v0.12 schema: migration script + PG DDL for overlay tables + NRC columns
```

### B. Files Modified During Incident Response

| File | Changes |
|------|---------|
| `app.py` | Pool config (3 iterations), `/health` endpoint (503 on failure) |
| `CHANGELOG.md` | v0.12.1, v0.12.2, v0.12.3 entries |
| `docs/OPERATIONS.md` | Incident log (3 entries), §2.1 prevention, §3 health behavior |
| `tests/conftest.py` | `_FakePool.check_connection` stub (v0.12.1) |

### C. Azure CLI Commands Used

```bash
# Restart App Service (3 times)
az webapp restart --name ufosint-explorer --resource-group rg-ufosint-prod

# Enable Always On
az webapp config set --name ufosint-explorer --resource-group rg-ufosint-prod --always-on true

# Enable Health Check
az webapp config set --name ufosint-explorer --resource-group rg-ufosint-prod \
    --generic-configurations '{"healthCheckPath": "/health"}'

# Verify configuration
az webapp config show --name ufosint-explorer --resource-group rg-ufosint-prod \
    --query "{alwaysOn:alwaysOn, healthCheckPath:healthCheckPath}"

# Check PG state
az postgres flexible-server show --name ufosint-pg --resource-group rg-ufosint-prod \
    --query "{state:state, tier:sku.tier}"

# Pull Health Check metrics
az monitor metrics list --resource-group rg-ufosint-prod \
    --resource ufosint-explorer --resource-type Microsoft.Web/sites \
    --metric "HealthCheckStatus" --interval PT1H
```

### D. How to Verify the Fix

**Overnight test (recommended):**
1. Check Health Check metrics tomorrow morning:
   ```bash
   az monitor metrics list --resource-group rg-ufosint-prod \
       --resource ufosint-explorer --resource-type Microsoft.Web/sites \
       --metric "HealthCheckStatus" --interval PT1H \
       --start-time $(date -u -d '-12 hours' +%Y-%m-%dT%H:00:00Z) \
       --end-time $(date -u +%Y-%m-%dT%H:00:00Z) \
       --query "value[0].timeseries[0].data[].{time:timeStamp, avg:average}" -o table
   ```
2. All hours should show 100%. Any drop below 100% indicates the
   fix is incomplete.

**Manual simulation (if you want to test now):**
1. Pause the PG server:
   ```bash
   az postgres flexible-server stop --name ufosint-pg --resource-group rg-ufosint-prod
   ```
2. Wait 30s, then check `/health`:
   ```bash
   curl -sS -w "\nHTTP:%{http_code}\n" https://ufosint.com/health
   ```
3. Should return HTTP 503 with `{"status":"unhealthy","detail":"..."}`.
4. Restart PG:
   ```bash
   az postgres flexible-server start --name ufosint-pg --resource-group rg-ufosint-prod
   ```
5. Wait 60s, restart App Service, verify `/health` returns 200.

⚠️ **Warning:** This simulation causes real downtime. Only do it
if you're comfortable with 2–3 minutes of outage.

### E. Key Azure Documentation

- [Azure NAT Gateway idle timeout](https://learn.microsoft.com/en-us/azure/nat-gateway/nat-gateway-resource#idle-timeout-timers)
- [App Service Health Check](https://learn.microsoft.com/en-us/azure/app-service/monitor-instances-health-check)
- [App Service Always On](https://learn.microsoft.com/en-us/azure/app-service/configure-common#configure-general-settings)
- [PostgreSQL Flexible Server connection limits](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-limits)

### F. TCP Keepalive Parameters Explained

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `keepalives` | 1 | Enable TCP keepalive on this connection |
| `keepalives_idle` | 60 | Seconds of idle before first probe |
| `keepalives_interval` | 10 | Seconds between subsequent probes |
| `keepalives_count` | 5 | Number of failed probes before declaring dead |
| **Detection time** | **~110s** | 60 + (10 × 5) = worst case |
| **NAT keep-warm interval** | **60s** | Probe every 60s keeps Azure's 4-min NAT mapping alive |

vs. Linux defaults:

| Parameter | Default | Problem |
|-----------|---------|---------|
| `tcp_keepalive_time` | 7200 (2h) | Way past Azure's 4-min NAT timeout |
| `tcp_keepalive_intvl` | 75 | Reasonable, but irrelevant if first probe is at 2h |
| `tcp_keepalive_probes` | 9 | Total detection time: 2h + 9×75s ≈ 2h 11m |

---

*End of report.*
