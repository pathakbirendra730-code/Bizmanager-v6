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
from datetime import datetime
from modules.app_admin.routes import app_admin_bp, app_admin_required, super_admin_required
from models.saas_auth import saas_fetchone, saas_fetchall, saas_execute, _is_postgres
from utils.saas_helpers import audit_log, validate_csrf, validate_mobile

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


@app_admin_bp.route("/users/<int:user_id>")
@app_admin_required
def view_user(user_id):
    """Detail view of a single SaaS user — their profile plus every
    business they belong to and in what role."""
    p = P()
    user = saas_fetchone(f"SELECT * FROM saas_users WHERE id={p}", (user_id,))
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("app_admin.all_users"))

    memberships = saas_fetchall(
        f"""SELECT b.id as business_id, b.name as business_name, ur.role, ur.is_active
            FROM saas_user_roles ur
            JOIN saas_businesses b ON b.id = ur.business_id
            WHERE ur.user_id={p}
            ORDER BY b.name ASC""",
        (user_id,)
    )
    return render_template("app_admin/view_user.html", user=user, memberships=memberships)


@app_admin_bp.route("/users/<int:user_id>/edit", methods=["POST"])
@super_admin_required
def edit_user(user_id):
    """Edit a SaaS user's name/email/mobile/active status. Sensitive —
    restricted to super admins, same as admin management."""
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error.", "danger")
        return redirect(url_for("app_admin.view_user", user_id=user_id))

    p = P()
    user = saas_fetchone(f"SELECT * FROM saas_users WHERE id={p}", (user_id,))
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("app_admin.all_users"))

    full_name = request.form.get("full_name", "").strip()
    email     = request.form.get("email", "").strip().lower()
    mobile_in = request.form.get("mobile", "").strip()

    if not full_name or len(full_name) < 2:
        flash("Full name must be at least 2 characters.", "danger")
        return redirect(url_for("app_admin.view_user", user_id=user_id))
    if not email or "@" not in email:
        flash("Please enter a valid email address.", "danger")
        return redirect(url_for("app_admin.view_user", user_id=user_id))

    ok, mobile_norm = validate_mobile(mobile_in)
    if not ok:
        flash(mobile_norm, "danger")
        return redirect(url_for("app_admin.view_user", user_id=user_id))

    dupe = saas_fetchone(
        f"SELECT id FROM saas_users WHERE (email={p} OR mobile={p}) AND id != {p}",
        (email, mobile_norm, user_id)
    )
    if dupe:
        flash("Another account already uses that email or mobile number.", "danger")
        return redirect(url_for("app_admin.view_user", user_id=user_id))

    saas_execute(
        f"""UPDATE saas_users
            SET full_name={p}, email={p}, mobile={p}, updated_at={p}
            WHERE id={p}""",
        (full_name, email, mobile_norm, datetime.utcnow().isoformat(), user_id)
    )
    audit_log("app_admin_edited_user", detail=f"user_id={user_id}")
    flash("User updated successfully.", "success")
    return redirect(url_for("app_admin.view_user", user_id=user_id))


@app_admin_bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@app_admin_required
def toggle_user(user_id):
    """Activate/deactivate a SaaS user (blocks their login without
    deleting their data — the safer option for most cases)."""
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error.", "danger")
        return redirect(url_for("app_admin.view_user", user_id=user_id))

    p = P()
    user = saas_fetchone(f"SELECT * FROM saas_users WHERE id={p}", (user_id,))
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("app_admin.all_users"))

    new_status = False if user["is_active"] else True
    saas_execute(
        f"UPDATE saas_users SET is_active={p} WHERE id={p}",
        (new_status, user_id)
    )
    audit_log("app_admin_toggled_user", detail=f"user_id={user_id} is_active={new_status}")
    flash(f"{user['full_name']} {'activated' if new_status else 'deactivated'}.", "success")
    return redirect(url_for("app_admin.view_user", user_id=user_id))


# Tables/columns that reference saas_users(id) WITHOUT ON DELETE CASCADE.
# These must be nulled out before a user row can be deleted, or PostgreSQL
# (and SQLite with foreign_keys=ON) rejects the DELETE with a foreign key
# violation — which is exactly what caused the 500 here. Deliberately
# NULL rather than delete the referencing rows: a demo/test account being
# removed shouldn't take real business data (invoices, ledger entries,
# audit history) down with it — it just becomes "created by: unknown".
_USER_FK_NULLABLE = [
    ("saas_businesses",      "created_by"),
    ("saas_user_roles",      "invited_by"),
    ("saas_audit_logs",      "user_id"),
    ("saas_pending_invites", "invited_by"),
    ("saas_pending_invites", "accepted_by"),
    ("saas_invoices",        "created_by"),
    ("saas_payments",        "created_by"),
    ("saas_expenses",        "created_by"),
    ("saas_purchases",       "created_by"),
    ("saas_ledger",          "created_by"),
    ("saas_cash_book",       "created_by"),
    ("saas_bank_book",       "created_by"),
    ("saas_journal_entries", "created_by"),
]


@app_admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@super_admin_required
def delete_user(user_id):
    """Permanently delete a SaaS user account (e.g. demo/test accounts).
    Blocked if the user is the SOLE owner of any business, since deleting
    them would leave that business with nobody able to manage it — reassign
    ownership or delete the business first in that case."""
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error.", "danger")
        return redirect(url_for("app_admin.all_users"))

    p = P()
    user = saas_fetchone(f"SELECT * FROM saas_users WHERE id={p}", (user_id,))
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("app_admin.all_users"))

    owned = saas_fetchall(
        f"""SELECT b.id as business_id, b.name FROM saas_user_roles ur
            JOIN saas_businesses b ON b.id = ur.business_id
            WHERE ur.user_id={p} AND ur.role='owner' AND ur.is_active=TRUE""",
        (user_id,)
    )
    blocking = []
    for biz in owned:
        other_owners = saas_fetchone(
            f"""SELECT COUNT(*) as c FROM saas_user_roles
                WHERE business_id={p} AND role='owner' AND is_active=TRUE AND user_id != {p}""",
            (biz["business_id"], user_id)
        )["c"]
        if other_owners == 0:
            blocking.append(biz["name"])

    if blocking:
        flash("Cannot delete — sole owner of: " + ", ".join(blocking) +
              ". Reassign ownership or delete the business first.", "danger")
        return redirect(url_for("app_admin.view_user", user_id=user_id))

    try:
        for table, col in _USER_FK_NULLABLE:
            saas_execute(f"UPDATE {table} SET {col}=NULL WHERE {col}={p}", (user_id,))
        saas_execute(f"DELETE FROM saas_user_roles WHERE user_id={p}", (user_id,))
        saas_execute(f"DELETE FROM saas_users WHERE id={p}", (user_id,))
    except Exception as e:
        audit_log("app_admin_delete_user_failed", detail=f"user_id={user_id} error={e}")
        flash("Could not delete this account due to a database error. "
              "Please try again or contact support if this persists.", "danger")
        return redirect(url_for("app_admin.view_user", user_id=user_id))

    audit_log("app_admin_deleted_user",
              detail=f"user_id={user_id} mobile={user['mobile']} email={user['email']}")
    flash(f"{user['full_name']} deleted.", "success")
    return redirect(url_for("app_admin.all_users"))


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
