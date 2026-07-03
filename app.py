import os
"""
app.py  — BizManager Multi-Shop ERP  |  Flask Application Factory
==================================================================
Run:  python app.py
URL:  http://127.0.0.1:5000
"""

# Load .env file in development (no-op in production where env vars are set by Render)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, redirect, url_for, session
from config import ActiveConfig
from models.database import init_db

# ── Blueprint imports ──────────────────────────────────────────────────────────
from modules.saas_auth      import saas_auth_bp
from modules.app_admin      import app_admin_bp
from modules.unified_login  import unified_bp
from modules.saas_business  import saas_customers_bp, saas_products_bp, saas_suppliers_bp, saas_billing_bp, saas_purchase_bp, saas_finance_bp, saas_reports_bp, saas_gst_bp, saas_accounts_bp, saas_dashboard_bp


def create_app():
    app = Flask(__name__)
    app.secret_key               = ActiveConfig.SECRET_KEY
    app.config["SESSION_PERMANENT"]     = ActiveConfig.SESSION_PERMANENT
    app.config["SESSION_COOKIE_NAME"]   = ActiveConfig.SESSION_COOKIE_NAME
    app.config["MAX_CONTENT_LENGTH"]    = ActiveConfig.MAX_CONTENT_LENGTH

    @app.template_filter("inr")
    def inr(value):
        """Format a number the way Indian ledgers do: 1,23,45,678 (not
        1,234,5678 Western-style) — grouped in 2s after the first 3 digits."""
        try:
            n = float(value or 0)
        except (TypeError, ValueError):
            return "0"
        neg = n < 0
        n = abs(n)
        whole = int(n)
        paise = round((n - whole) * 100)
        s = str(whole)
        if len(s) > 3:
            head, tail = s[:-3], s[-3:]
            parts = []
            while len(head) > 2:
                parts.insert(0, head[-2:])
                head = head[:-2]
            if head:
                parts.insert(0, head)
            s = ",".join(parts) + "," + tail
        out = f"{s}.{paise:02d}"
        return f"-{out}" if neg else out

    # ── Register blueprints ────────────────────────────────────────────────────
    app.register_blueprint(saas_auth_bp)                          # prefix /saas built-in
    app.register_blueprint(app_admin_bp)                          # prefix /app-admin built-in
    app.register_blueprint(unified_bp)                            # /login — single entry point
    app.register_blueprint(saas_customers_bp)                     # /biz/customers — SaaS-native
    app.register_blueprint(saas_products_bp)                      # /biz/products — SaaS-native
    app.register_blueprint(saas_suppliers_bp)                     # /biz/suppliers — SaaS-native
    app.register_blueprint(saas_billing_bp)                       # /biz/billing — SaaS-native
    app.register_blueprint(saas_purchase_bp)                      # /biz/purchase — SaaS-native
    app.register_blueprint(saas_finance_bp)                       # /biz/finance — SaaS-native
    app.register_blueprint(saas_reports_bp)                       # /biz/reports — SaaS-native
    app.register_blueprint(saas_gst_bp)                           # /biz/gst — SaaS-native
    app.register_blueprint(saas_accounts_bp)                      # /biz/accounts — SaaS-native
    app.register_blueprint(saas_dashboard_bp)                     # /biz/dashboard — SaaS-native

    # ── Root redirect ──────────────────────────────────────────────────────────
    @app.route("/")
    def index():
        if "saas_user_id" in session:
            return redirect(url_for("saas_dashboard.index"))
        if session.get("admin_id"):
            return redirect(url_for("app_admin.dashboard"))
        # Default to the unified login for new visitors
        return redirect(url_for("unified_login.login"))

    # ── Health check (required by Render) ────────────────────────────────────
    @app.route("/health")
    def health():
        from flask import jsonify
        from datetime import datetime
        try:
            from models.database import get_db
            conn = get_db()
            conn.execute("SELECT 1").fetchone()
            conn.close()
            db_ok = True
        except Exception:
            db_ok = False
        return jsonify({
            "status":    "ok" if db_ok else "degraded",
            "db":        "ok" if db_ok else "error",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "env":       os.environ.get("APP_ENV", "development")
        }), 200 if db_ok else 500

    # ── Security headers ──────────────────────────────────────────────────────
    @app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"]        = "SAMEORIGIN"
        response.headers["X-XSS-Protection"]       = "1; mode=block"
        response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
        return response

    # ── Context processor: injects into every template ──────────────────────
    @app.context_processor
    def inject_globals():
        from utils.saas_helpers import generate_csrf_token

        # Ensure csrf_token is always available, in every blueprint's templates —
        # previously only saas_auth_bp/app_admin_bp injected this via their own
        # context_processor, leaving every other blueprint (including new SaaS
        # business modules) silently rendering an empty token that happened to
        # pass validate_csrf() by accident (empty == empty). Centralising here
        # closes that gap for all current and future blueprints.
        csrf_token = generate_csrf_token()

        # Low-stock count badge for sidebar — tenant-scoped, SaaS-native table
        low_stock_count = 0
        biz_id = session.get("saas_business_id")
        if biz_id:
            from models.saas_auth import saas_fetchone, _is_postgres
            p = "%s" if _is_postgres() else "?"
            row = saas_fetchone(
                f"""SELECT COUNT(*) as cnt FROM saas_products
                    WHERE business_id={p} AND stock_quantity<=low_stock_threshold
                      AND is_active=1""",
                (biz_id,)
            )
            low_stock_count = row["cnt"] if row else 0

        return {
            # CSRF token — available in every blueprint's templates
            "csrf_token": csrf_token,

            # SaaS auth context (available in all templates)
            "saas_user_id":     session.get("saas_user_id"),
            "saas_fullname":    session.get("saas_fullname", ""),
            "saas_role":        session.get("saas_role", ""),
            "saas_business_id": session.get("saas_business_id"),
            "saas_biz_name":    session.get("saas_biz_name", ""),
            "saas_biz_plan":    session.get("saas_biz_plan", "free"),
            "low_stock_count":  low_stock_count,
        }

    # ── DB init ────────────────────────────────────────────────────────────────
    with app.app_context():
        init_db()
        # SaaS Auth tables (SQLite dev / PostgreSQL prod)
        from models.saas_auth import init_saas_db
        init_saas_db()
        from models.saas_business_data import init_saas_business_tables
        init_saas_business_tables()
        from models.saas_ledger_engine import init_ledger_engine_tables
        init_ledger_engine_tables()

    return app


if __name__ == "__main__":
    app = create_app()
    print("\n" + "═"*55)
    print("  BizManager v6 — SaaS")
    print("  http://127.0.0.1:5000")
    print("═"*55 + "\n")
    app.run(host=ActiveConfig.HOST, port=ActiveConfig.PORT, debug=ActiveConfig.DEBUG)
