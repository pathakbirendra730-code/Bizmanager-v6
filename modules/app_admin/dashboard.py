"""
modules/app_admin/dashboard.py — Platform-wide Admin Views
=============================================================
The app admin sees EVERYTHING across the platform:
  • All registered SaaS users (saas_users table)
  • All legacy ERP users (users table, if still in use)
  • All businesses and their team rosters
  • Pending invites
  • Global audit log

This is intentionally separate from the per-business `saas_auth.team`
view, which only shows members of the CURRENT business.
"""

from flask import render_template, request, redirect, url_for, flash, session
from modules.app_admin.routes import app_admin_bp, app_admin_required, super_admin_required
from models.saas_auth import saas_fetchone, saas_fetchall, saas_execute, _is_postgres
from utils.saas_helpers import audit_log, validate_csrf

P = lambda: "%s" if _is_postgres() else "?"


@app_admin_bp.route("/dashboard")
@app_admin_required
def dashboard():
    p = P()

    total_businesses = saas_fetchone(
        "SELECT COUNT(*) as c FROM saas_businesses WHERE is_active=TRUE"
    )["c"]
    total_saas_users = saas_fetchone(
        "SELECT COUNT(*) as c FROM saas_users WHERE is_active=TRUE"
    )["c"]
    verified_users = saas_fetchone(
        "SELECT COUNT(*) as c FROM saas_users WHERE is_verified=TRUE"
    )["c"]
    pending_invites = saas_fetchone(
        f"SELECT COUNT(*) as c FROM saas_pending_invites WHERE status='pending'"
    )["c"]

    recent_signups = saas_fetchall(
        "SELECT * FROM saas_users ORDER BY created_at DESC LIMIT 8"
    )
    recent_businesses = saas_fetchall(
        "SELECT * FROM saas_businesses ORDER BY created_at DESC LIMIT 8"
    )

    stats = {
        "total_businesses": total_businesses,
        "total_saas_users": total_saas_users,
        "verified_users":   verified_users,
        "pending_invites":  pending_invites,
    }

    return render_template("app_admin/dashboard.html",
                           stats=stats,
                           recent_signups=recent_signups,
                           recent_businesses=recent_businesses)


# ════════════════════════════ ALL USERS (UNIFIED) ════════════════════════════

@app_admin_bp.route("/users")
@app_admin_required
def all_users():
    """
    All SaaS users, with their business memberships and roles.
    """
    search = request.args.get("q", "").strip()
    p = P()

    # ── SaaS users with their business memberships ───────────────────────────
    if search:
        like = f"%{search}%"
        saas_users = saas_fetchall(
            f"""SELECT * FROM saas_users
                WHERE full_name LIKE {p} OR mobile LIKE {p} OR email LIKE {p}
                ORDER BY created_at DESC""",
            (like, like, like)
        )
    else:
        saas_users = saas_fetchall(
            "SELECT * FROM saas_users ORDER BY created_at DESC"
        )

    # Attach business memberships to each SaaS user
    for u in saas_users:
        memberships = saas_fetchall(
            f"""SELECT b.name as business_name, b.id as business_id, ur.role
                FROM saas_user_roles ur
                JOIN saas_businesses b ON b.id = ur.business_id
                WHERE ur.user_id={p} AND ur.is_active=TRUE""",
            (u["id"],)
        )
        u["memberships"] = memberships
        u["source"] = "saas"

    return render_template("app_admin/all_users.html",
                           saas_users=saas_users,
                           search=search,
                           total_count=len(saas_users))


# ════════════════════════════ ALL BUSINESSES ═════════════════════════════════

@app_admin_bp.route("/businesses")
@app_admin_required
def all_businesses():
    p = P()
    businesses = saas_fetchall(
        "SELECT * FROM saas_businesses ORDER BY created_at DESC"
    )
    for b in businesses:
        member_count = saas_fetchone(
            f"SELECT COUNT(*) as c FROM saas_user_roles WHERE business_id={p} AND is_active=TRUE",
            (b["id"],)
        )["c"]
        b["member_count"] = member_count

    return render_template("app_admin/all_businesses.html", businesses=businesses)


@app_admin_bp.route("/businesses/<int:biz_id>/toggle", methods=["POST"])
@app_admin_required
def toggle_business(biz_id):
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error.", "danger")
        return redirect(url_for("app_admin.all_businesses"))

    p = P()
    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))
    if not biz:
        flash("Business not found.", "danger")
        return redirect(url_for("app_admin.all_businesses"))

    # Native Python booleans, not 1/0 — psycopg2 rejects an integer literal
    # bound to a BOOLEAN column (SQLite is lenient about this, PostgreSQL is not).
    new_status = False if biz["is_active"] else True
    saas_execute(
        f"UPDATE saas_businesses SET is_active={p} WHERE id={p}",
        (new_status, biz_id)
    )
    audit_log("app_admin_business_toggled",
              entity_type="business", entity_id=str(biz_id),
              detail=f"is_active={new_status} by_admin={session.get('admin_userid')}")
    flash(f"Business {'activated' if new_status else 'deactivated'}.", "success")
    return redirect(url_for("app_admin.all_businesses"))


# ════════════════════════════ PENDING INVITES (PLATFORM VIEW) ════════════════

@app_admin_bp.route("/invites")
@app_admin_required
def all_invites():
    invites = saas_fetchall(
        """SELECT i.*, b.name as business_name
           FROM saas_pending_invites i
           JOIN saas_businesses b ON b.id = i.business_id
           ORDER BY i.created_at DESC"""
    )
    return render_template("app_admin/all_invites.html", invites=invites)


# ════════════════════════════ PLATFORM SETTINGS ══════════════════════════════
#
# Runtime-configurable behavior that used to require an env var change +
# redeploy — e.g. whether signup requires mobile OTP, which email/SMS
# provider is active. Restricted to super admins since these affect every
# business on the platform, not just one.

@app_admin_bp.route("/settings", methods=["GET", "POST"])
@super_admin_required
def platform_settings():
    from utils.platform_settings import SETTINGS_SCHEMA, all_settings, set_setting

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error. Please try again.", "danger")
            return redirect(url_for("app_admin.platform_settings"))

        admin_id = session.get("admin_id")
        errors = []
        for schema in SETTINGS_SCHEMA:
            key = schema["key"]
            if schema["type"] == "bool":
                # Unchecked checkboxes simply don't appear in form data.
                value = "true" if request.form.get(key) == "on" else "false"
            elif schema["type"] == "secret":
                # Blank field means "leave the stored value alone" — the
                # real secret is never sent to the browser, so an empty
                # submission is the normal case (admin didn't intend to
                # change it), not a request to clear it.
                value = request.form.get(key, "").strip()
                if not value:
                    continue
            else:
                value = request.form.get(key, "").strip()
                if schema.get("options") and value not in schema["options"]:
                    continue  # ignore tampered/invalid values, keep old one
            try:
                set_setting(key, value, updated_by=admin_id)
            except ValueError as e:
                errors.append(str(e))

        if errors:
            for e in errors:
                flash(e, "danger")
            return redirect(url_for("app_admin.platform_settings"))

        audit_log("platform_settings_updated",
                  detail=f"by_admin={session.get('admin_userid')}")
        flash("Settings saved.", "success")
        return redirect(url_for("app_admin.platform_settings"))

    return render_template("app_admin/settings.html", settings=all_settings())
