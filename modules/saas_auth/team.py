"""
modules/saas_auth/team.py — Team / User Management Routes
==========================================================
Blueprint: saas_auth  (same blueprint, mounted at /saas/team)

Routes:
  GET       /saas/team            — list team members
  GET/POST  /saas/team/invite     — invite user by mobile + role
  POST      /saas/team/remove     — remove a team member
  POST      /saas/team/role       — change a member's role
"""

from flask import render_template, request, redirect, url_for, flash, session
from modules.saas_auth.routes import saas_auth_bp
from utils.saas_helpers import (
    saas_login_required,
    audit_log, generate_csrf_token, validate_csrf
)
from utils.saas_middleware import (
    owner_only, manager_or_above,
    invite_user_to_business, get_business_members, remove_user_from_business,
    ROLE_RANK
)
from models.saas_auth import saas_execute, saas_fetchone, _is_postgres

P = lambda: "%s" if _is_postgres() else "?"


def _get_tenant():
    bid = session.get("saas_business_id")
    if not bid:
        flash("No active business. Please select one.", "warning")
        return None
    return int(bid)


@saas_auth_bp.route("/team")
@saas_login_required
def team():
    biz_id = _get_tenant()
    if not biz_id:
        return redirect(url_for("saas_auth.select_business"))

    members = get_business_members(biz_id)
    biz = saas_fetchone(
        f"SELECT * FROM saas_businesses WHERE id={P()}", (biz_id,)
    )
    from models.saas_auth import saas_fetchall
    pending_invites = saas_fetchall(
        f"""SELECT * FROM saas_pending_invites
            WHERE business_id={P()} AND status='pending'
            ORDER BY created_at DESC""",
        (biz_id,)
    )
    return render_template("saas_auth/team.html",
                           members=members, biz=biz,
                           pending_invites=pending_invites,
                           roles=list(ROLE_RANK.keys()))


@saas_auth_bp.route("/team/invite", methods=["GET", "POST"])
@saas_login_required
def team_invite():
    biz_id = _get_tenant()
    if not biz_id:
        return redirect(url_for("saas_auth.select_business"))

    if session.get("saas_role") not in ("owner", "manager"):
        flash("Only owners and managers can invite team members.", "danger")
        return redirect(url_for("saas_auth.team"))

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error.", "danger")
            return redirect(url_for("saas_auth.team_invite"))

        mobile = request.form.get("mobile", "").strip()
        email  = request.form.get("email", "").strip().lower()
        role   = request.form.get("role", "staff")

        if role not in ROLE_RANK:
            flash("Invalid role selected.", "danger")
            return redirect(url_for("saas_auth.team_invite"))

        # Prevent inviting owner — only one owner per business
        if role == "owner":
            flash("You cannot invite another owner. Transfer ownership instead.", "danger")
            return redirect(url_for("saas_auth.team_invite"))

        if not mobile and not email:
            flash("Enter a mobile number or email address to invite.", "danger")
            return render_template("saas_auth/team_invite.html",
                                   roles=[r for r in ROLE_RANK if r != "owner"])

        mobile_norm = ""
        if mobile:
            from utils.saas_helpers import validate_mobile
            ok, mobile_norm = validate_mobile(mobile)
            if not ok:
                flash(mobile_norm, "danger")
                return render_template("saas_auth/team_invite.html",
                                       mobile=mobile, email=email,
                                       roles=[r for r in ROLE_RANK if r != "owner"])

        inviter_id = session.get("saas_user_id")
        p = P()

        # ── Look up an existing VERIFIED account by mobile or email ──────────
        existing_user = None
        if mobile_norm:
            existing_user = saas_fetchone(
                f"SELECT * FROM saas_users WHERE mobile={p} AND is_verified=TRUE", (mobile_norm,)
            )
        if not existing_user and email:
            existing_user = saas_fetchone(
                f"SELECT * FROM saas_users WHERE email={p} AND is_verified=TRUE", (email,)
            )

        if existing_user:
            # ── Person already has an account — attach them immediately ─────
            result = invite_user_to_business(inviter_id, biz_id, existing_user["mobile"], role)
            if result["ok"]:
                flash(result["message"], "success")
            else:
                flash(result["message"], "danger")
            return redirect(url_for("saas_auth.team"))

        # ── No account yet — create a PENDING invite. When this person  ─────
        # ── signs up with the matching mobile/email, they auto-join.    ─────
        from datetime import datetime, timedelta
        expires = (datetime.utcnow() + timedelta(days=14)).isoformat()

        # Avoid duplicate pending invites for the same contact + business
        dup = saas_fetchone(
            f"""SELECT id FROM saas_pending_invites
                WHERE business_id={p} AND status='pending'
                AND (mobile={p} OR email={p})""",
            (biz_id, mobile_norm, email)
        )
        if dup:
            flash("An invite is already pending for this contact.", "warning")
            return redirect(url_for("saas_auth.team"))

        saas_execute(
            f"""INSERT INTO saas_pending_invites
                (business_id, mobile, email, role, invited_by, expires_at)
                VALUES ({p},{p},{p},{p},{p},{p})""",
            (biz_id, mobile_norm, email, role, inviter_id, expires)
        )
        audit_log("invite_created_pending", user_id=inviter_id, business_id=biz_id,
                  detail=f"mobile={mobile_norm} email={email} role={role}")

        contact = mobile_norm or email
        flash(f"Invite sent! {contact} will join automatically as {role} "
              f"once they sign up at BizManager (valid 14 days).", "success")
        return redirect(url_for("saas_auth.team"))

    return render_template("saas_auth/team_invite.html",
                           roles=[r for r in ROLE_RANK if r != "owner"])


@saas_auth_bp.route("/team/invite/<int:invite_id>/revoke", methods=["POST"])
@saas_login_required
def team_revoke_invite(invite_id):
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error.", "danger")
        return redirect(url_for("saas_auth.team"))

    if session.get("saas_role") not in ("owner", "manager"):
        flash("Only owners and managers can revoke invites.", "danger")
        return redirect(url_for("saas_auth.team"))

    biz_id = _get_tenant()
    p = P()
    invite = saas_fetchone(
        f"SELECT * FROM saas_pending_invites WHERE id={p} AND business_id={p}",
        (invite_id, biz_id)
    )
    if not invite:
        flash("Invite not found.", "danger")
        return redirect(url_for("saas_auth.team"))

    saas_execute(
        f"UPDATE saas_pending_invites SET status='revoked' WHERE id={p}",
        (invite_id,)
    )
    audit_log("invite_revoked", business_id=biz_id,
              entity_type="invite", entity_id=str(invite_id))
    flash("Invite revoked.", "success")
    return redirect(url_for("saas_auth.team"))


@saas_auth_bp.route("/team/remove", methods=["POST"])
@saas_login_required
def team_remove():
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error.", "danger")
        return redirect(url_for("saas_auth.team"))

    if session.get("saas_role") != "owner":
        flash("Only the owner can remove members.", "danger")
        return redirect(url_for("saas_auth.team"))

    biz_id  = _get_tenant()
    user_id = request.form.get("user_id", type=int)
    owner_id = session.get("saas_user_id")

    if not user_id or not biz_id:
        flash("Invalid request.", "danger")
        return redirect(url_for("saas_auth.team"))

    result = remove_user_from_business(owner_id, user_id, biz_id)
    flash(result["message"], "success" if result["ok"] else "danger")
    return redirect(url_for("saas_auth.team"))


@saas_auth_bp.route("/team/role", methods=["POST"])
@saas_login_required
def team_change_role():
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error.", "danger")
        return redirect(url_for("saas_auth.team"))

    if session.get("saas_role") != "owner":
        flash("Only the owner can change roles.", "danger")
        return redirect(url_for("saas_auth.team"))

    biz_id  = _get_tenant()
    user_id = request.form.get("user_id", type=int)
    new_role = request.form.get("role", "")

    if new_role not in ROLE_RANK or new_role == "owner":
        flash("Invalid role.", "danger")
        return redirect(url_for("saas_auth.team"))

    p = P()
    saas_execute(
        f"UPDATE saas_user_roles SET role={p} WHERE user_id={p} AND business_id={p}",
        (new_role, user_id, biz_id)
    )
    audit_log("role_changed", business_id=biz_id,
              entity_type="user", entity_id=str(user_id),
              detail=f"new_role={new_role}")
    flash("Role updated successfully.", "success")
    return redirect(url_for("saas_auth.team"))
