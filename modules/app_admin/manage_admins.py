"""
modules/app_admin/manage_admins.py — App Admin Account Management
=====================================================================
Only an existing app admin with is_super=1 can create another app admin.
There is NO other way to create one (besides the one-time seed script).
"""

from flask import render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash
from modules.app_admin.routes import app_admin_bp, super_admin_required
from models.saas_auth import saas_fetchone, saas_fetchall, saas_execute, _is_postgres
from utils.saas_helpers import audit_log, validate_csrf

P = lambda: "%s" if _is_postgres() else "?"


@app_admin_bp.route("/admins")
@super_admin_required
def list_admins():
    admins = saas_fetchall("SELECT * FROM app_admins ORDER BY created_at ASC")
    return render_template("app_admin/list_admins.html", admins=admins)


@app_admin_bp.route("/admins/create", methods=["GET", "POST"])
@super_admin_required
def create_admin():
    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error.", "danger")
            return redirect(url_for("app_admin.create_admin"))

        user_id   = request.form.get("user_id", "").strip()
        password  = request.form.get("password", "")
        confirm   = request.form.get("confirm_password", "")
        full_name = request.form.get("full_name", "").strip()
        email     = request.form.get("email", "").strip().lower()
        mobile    = request.form.get("mobile", "").strip()
        is_super  = 1 if request.form.get("is_super") == "on" else 0

        errors = []
        if not user_id or len(user_id) < 3:
            errors.append("User ID must be at least 3 characters.")
        if not full_name:
            errors.append("Full name is required.")
        if not email or "@" not in email:
            errors.append("A valid email is required (used for OTP).")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")

        p = P()
        if saas_fetchone(f"SELECT id FROM app_admins WHERE user_id={p}", (user_id,)):
            errors.append("This User ID is already taken.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("app_admin/create_admin.html",
                                   user_id=user_id, full_name=full_name,
                                   email=email, mobile=mobile)

        saas_execute(
            f"""INSERT INTO app_admins
                (user_id, password_hash, full_name, email, mobile, is_super)
                VALUES ({p},{p},{p},{p},{p},{p})""",
            (user_id, generate_password_hash(password), full_name, email, mobile, is_super)
        )
        audit_log("app_admin_created",
                  detail=f"new_admin={user_id} is_super={is_super} "
                         f"created_by={session.get('admin_userid')}")
        flash(f"App admin '{user_id}' created successfully.", "success")
        return redirect(url_for("app_admin.list_admins"))

    return render_template("app_admin/create_admin.html")


@app_admin_bp.route("/admins/<int:admin_id>/toggle", methods=["POST"])
@super_admin_required
def toggle_admin(admin_id):
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error.", "danger")
        return redirect(url_for("app_admin.list_admins"))

    if admin_id == session.get("admin_id"):
        flash("You cannot deactivate your own account.", "danger")
        return redirect(url_for("app_admin.list_admins"))

    p = P()
    admin = saas_fetchone(f"SELECT * FROM app_admins WHERE id={p}", (admin_id,))
    if not admin:
        flash("Admin not found.", "danger")
        return redirect(url_for("app_admin.list_admins"))

    new_status = 0 if admin["is_active"] else 1
    saas_execute(
        f"UPDATE app_admins SET is_active={p} WHERE id={p}",
        (new_status, admin_id)
    )
    audit_log("app_admin_toggled",
              detail=f"target={admin['user_id']} is_active={new_status} "
                     f"by={session.get('admin_userid')}")
    flash(f"Admin '{admin['user_id']}' {'activated' if new_status else 'deactivated'}.",
          "success")
    return redirect(url_for("app_admin.list_admins"))
