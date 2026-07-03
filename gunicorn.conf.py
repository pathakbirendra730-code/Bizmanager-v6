# gunicorn.conf.py — Production server config for Render
import os

# ── Binding ────────────────────────────────────────────────────────────────────
# Render sets PORT env var automatically
port   = os.environ.get("PORT", "10000")
bind   = f"0.0.0.0:{port}"

# ── Workers ────────────────────────────────────────────────────────────────────
# 2 workers per CPU is a safe default for Render's free/starter tier
workers     = int(os.environ.get("WEB_CONCURRENCY", 2))
worker_class = "sync"          # use "gevent" if you add gevent to requirements
threads     = 2
timeout     = 120              # seconds before worker is killed (increase for heavy tasks)
keepalive   = 5

# ── Logging ───────────────────────────────────────────────────────────────────
accesslog  = "-"               # stdout → visible in Render dashboard
errorlog   = "-"               # stderr → visible in Render dashboard
loglevel   = "info"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sµs'

# ── Process ───────────────────────────────────────────────────────────────────
preload_app = True             # load app once, fork workers (saves memory)
max_requests = 1000            # restart worker after N requests (memory leak guard)
max_requests_jitter = 100      # randomise restarts to avoid thundering herd
