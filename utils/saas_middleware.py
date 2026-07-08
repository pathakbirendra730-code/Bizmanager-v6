"""
utils/saas_middleware.py — Multi-Tenant Guards & Role Middleware
===============================================================
Use these in routes that must be isolated per business:

    from utils.saas_middleware import tenant_scope, owner_only, can_access_finance

Every data query should be scoped to the current saas_business_id to enforce
multi-tenancy. Use `assert_tenant_access()` as a quick guard.

Role hierarchy:
    owner  > manager > accountant > staff

Permission matrix:
    Feature          | owner | manager | accountant | staff
    ─────────────────┼───────┼─────────┼────────────┼──────
    View dashboard   |  ✓    |   ✓     |    ✓       |  ✓
    Create invoices  |  ✓    |   ✓     |    ✗       |  ✗
    Manage inventory |  ✓    |   ✓     |    ✗       |  ✗
    View reports     |  ✓    |   ✓     |    ✓       |  ✗
    Finance / GL     |  ✓    |   ✗     |    ✓       |  ✗
    Manage users     |  ✓    |   ✗     |    ✗       |  ✗
    Business settings|  ✓    |   ✗     |    ✗       |  ✗
    GST returns      |  ✓    |   ✓     |    ✓       |  ✗
"""

from functools import wraps
from flask import session, redirect, url_for, flash, abort, request, g
from utils.saas_helpers import (
    SAAS_SESSION_KEY, SAAS_BIZ_KEY, SAAS_ROLE_KEY,
    audit_log, saas_fetchone
)

# ── Role hierarchy ────────────────────────────────────────────────────────────
ROLE_RANK = {
    "owner":      100,
    "manager":    70,
    "accountant": 50,
    "staff":      20,
}

PERMISSIONS = {
    # action_key: minimum role needed
    "view_dashboard":    "staff",
    "view_invoice":      "staff",
    "create_invoice":    "manager",
    "edit_invoice":      "manager",
    "delete_invoice":    "owner",
    "view_inventory":    "staff",
    "manage_inventory":  "manager",
    "view_customers":    "staff",
    "manage_customers":  "manager",
    "view_reports":      "accountant",
    "view_finance":      "accountant",
    "manage_finance":    "accountant",
    "view_gst":          "accountant",
    "manage_gst":        "accountant",
    "manage_users":      "owner",
    "business_settings": "owner",
    "view_purchase":     "manager",
    "manage_purchase":   "manager",
    "view_supplier":     "manager",
    "manage_supplier":   "manager",
    "backup_restore":    "owner",
}


# ── Core permission check ─────────────────────────────────────────────────────

def has_permission(action: str, role: str = None) -> bool:
    """Check if current session role (or supplied role) can perform an action."""
    role = role or session.get(SAAS_ROLE_KEY, "staff")
    required_role = PERMISSIONS.get(action, "owner")
    return ROLE_RANK.get(role, 0) >= ROLE_RANK.get(required_role, 100)


def current_role() -> str:
    return session.get(SAAS_ROLE_KEY, "staff")


def is_owner() -> bool:
    return current_role() == "owner"


def is_manager_or_above() -> bool:
    return ROLE_RANK.get(current_role(), 0) >= ROLE_RANK["manager"]


def is_accountant_or_above() -> bool:
    return ROLE_RANK.get(current_role(), 0) >= ROLE_RANK["accountant"]


# ── Decorator factories ───────────────────────────────────────────────────────

def permission_required(action: str):
    """
    Decorator: require a named permission.
    Usage:  @permission_required("manage_inventory")
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get(SAAS_SESSION_KEY):
                flash("Please log in.", "warning")
                return redirect(url_for("saas_auth.login"))
            if not has_permission(action):
                audit_log("permission_denied", status="failure",
                          detail=f"action={action} role={current_role()}")
                flash(f"You don't have permission to {action.replace('_', ' ')}.", "danger")
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator


def owner_only(f):
    """Decorator: restrict route to business owners."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get(SAAS_SESSION_KEY):
            return redirect(url_for("saas_auth.login"))
        if current_role() != "owner":
            flash("Only the business owner can access this.", "danger")
            abort(403)
        return f(*args, **kwargs)
    return decorated


def manager_or_above(f):
    """Decorator: owner or manager only."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get(SAAS_SESSION_KEY):
            return redirect(url_for("saas_auth.login"))
        if not is_manager_or_above():
            flash("Managers and above can access this.", "danger")
            abort(403)
        return f(*args, **kwargs)
    return decorated


def accountant_or_above(f):
    """Decorator: owner, manager, or accountant."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get(SAAS_SESSION_KEY):
            return redirect(url_for("saas_auth.login"))
        if not is_accountant_or_above():
            flash("Finance access is restricted.", "danger")
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ── Multi-tenant scope guard ──────────────────────────────────────────────────

def assert_tenant_access(resource_business_id: int):
    """
    Raise 403 if resource does not belong to the current session's business.
    Call this before returning any record to the user.

        biz_id = assert_tenant_access(invoice["business_id"])
    """
    current_biz = session.get(SAAS_BIZ_KEY)
    if not current_biz:
        abort(403)
    if int(resource_business_id) != int(current_biz):
        audit_log("tenant_breach_attempt", status="failure",
                  detail=f"requested_biz={resource_business_id} session_biz={current_biz}")
        abort(403)
    return current_biz


def get_tenant_id() -> int:
    """Return active business_id from session, or abort 403."""
    biz_id = session.get(SAAS_BIZ_KEY)
    if not biz_id:
        abort(403)
    return int(biz_id)


# ── User management helpers ───────────────────────────────────────────────────

def invite_user_to_business(inviter_id: int, business_id: int,
                             mobile: str, role: str) -> dict:
    """
    Invite an existing user to a business.
    Returns {"ok": bool, "message": str, "user_id": int|None}
    """
    from utils.saas_helpers import saas_fetchone, saas_execute, _is_postgres
    from models.saas_auth import _is_postgres as _pg

    p = "%s" if _pg() else "?"

    # Look up user
    user = saas_fetchone(
        f"SELECT * FROM saas_users WHERE mobile={p} AND is_active=TRUE AND is_verified=TRUE",
        (mobile,)
    )
    if not user:
        return {"ok": False, "message": "No verified account found with this mobile number.",
                "user_id": None}

    # Check if already a member
    existing = saas_fetchone(
        f"SELECT * FROM saas_user_roles WHERE user_id={p} AND business_id={p}",
        (user["id"], business_id)
    )
    if existing:
        if existing["is_active"]:
            return {"ok": False,
                    "message": f"This user is already a {existing['role']} of this business.",
                    "user_id": user["id"]}
        else:
            # Re-activate
            from models.saas_auth import saas_execute
            saas_execute(
                f"UPDATE saas_user_roles SET is_active=TRUE, role={p} WHERE user_id={p} AND business_id={p}",
                (role, user["id"], business_id)
            )
            return {"ok": True, "message": "User re-activated.", "user_id": user["id"]}

    from models.saas_auth import saas_execute
    saas_execute(
        f"INSERT INTO saas_user_roles (user_id, business_id, role, invited_by) VALUES ({p},{p},{p},{p})",
        (user["id"], business_id, role, inviter_id)
    )
    audit_log("user_invited", user_id=inviter_id, business_id=business_id,
              entity_type="user", entity_id=str(user["id"]),
              detail=f"mobile={mobile} role={role}")
    return {"ok": True, "message": f"{user['full_name']} added as {role}.",
            "user_id": user["id"]}


def get_business_members(business_id: int) -> list:
    """Return all active members of a business with their roles."""
    from models.saas_auth import saas_fetchall, _is_postgres
    p = "%s" if _is_postgres() else "?"
    return saas_fetchall(
        f"""SELECT u.id, u.full_name, u.mobile, u.email,
                   u.avatar_initials, u.last_login,
                   ur.role, ur.joined_at
            FROM saas_users u
            JOIN saas_user_roles ur ON ur.user_id = u.id
            WHERE ur.business_id = {p} AND ur.is_active=TRUE AND u.is_active=TRUE
            ORDER BY ur.role DESC, ur.joined_at ASC""",
        (business_id,)
    )


def remove_user_from_business(owner_id: int, user_id: int, business_id: int) -> dict:
    """Remove a user's access to a business. Owners cannot remove themselves."""
    if user_id == owner_id:
        return {"ok": False, "message": "You cannot remove yourself from the business."}

    from models.saas_auth import saas_execute, _is_postgres
    p = "%s" if _is_postgres() else "?"
    saas_execute(
        f"UPDATE saas_user_roles SET is_active=FALSE WHERE user_id={p} AND business_id={p}",
        (user_id, business_id)
    )
    audit_log("user_removed", user_id=owner_id, business_id=business_id,
              entity_type="user", entity_id=str(user_id))
    return {"ok": True, "message": "User access revoked."}
