#!/usr/bin/env python3
"""
scripts/create_app_admin.py — One-time App Admin Seed Script
================================================================
This is the ONLY way to create the first app admin account.
There is no web route for this — by design, for security.

Usage:
    cd bms/
    python scripts/create_app_admin.py

Run this once after deploying, then log in at /app-admin/login.
Subsequent admins can be created by an existing super-admin via the
web UI at /app-admin/admins/create.
"""

import sys
import os
import getpass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.security import generate_password_hash
from models.saas_auth import init_saas_db, saas_fetchone, saas_execute, _is_postgres

P = lambda: "%s" if _is_postgres() else "?"


def main():
    print("=" * 56)
    print("  BizManager — Create First App Admin")
    print("=" * 56)
    print()
    print("This account can log in at /app-admin/login")
    print("and will have FULL platform access (all businesses, all users).")
    print()

    init_saas_db()

    user_id = input("Admin User ID (e.g. 'admin'): ").strip()
    if not user_id or len(user_id) < 3:
        print("✗ User ID must be at least 3 characters. Aborting.")
        sys.exit(1)

    p = P()
    existing = saas_fetchone(f"SELECT id FROM app_admins WHERE user_id={p}", (user_id,))
    if existing:
        print(f"✗ User ID '{user_id}' already exists. Aborting.")
        sys.exit(1)

    full_name = input("Full Name: ").strip()
    if not full_name:
        print("✗ Full name is required. Aborting.")
        sys.exit(1)

    email = input("Email (required — used for OTP): ").strip().lower()
    if not email or "@" not in email:
        print("✗ A valid email is required. Aborting.")
        sys.exit(1)

    mobile = input("Mobile (optional, for production SMS OTP, format +91XXXXXXXXXX): ").strip()

    password  = getpass.getpass("Password (min 8 chars): ")
    if len(password) < 8:
        print("✗ Password must be at least 8 characters. Aborting.")
        sys.exit(1)

    confirm = getpass.getpass("Confirm Password: ")
    if password != confirm:
        print("✗ Passwords do not match. Aborting.")
        sys.exit(1)

    saas_execute(
        f"""INSERT INTO app_admins
            (user_id, password_hash, full_name, email, mobile, is_super, is_active)
            VALUES ({p},{p},{p},{p},{p},1,1)""",
        (user_id, generate_password_hash(password), full_name, email, mobile)
    )

    print()
    print("=" * 56)
    print(f"  ✅ App admin '{user_id}' created successfully!")
    print(f"  Role: Super Admin (can create other admins)")
    print(f"  Log in at: /app-admin/login")
    print("=" * 56)


if __name__ == "__main__":
    main()
