from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pyotp
import pytest
from sqlalchemy import select, text

from app import db, emailer
from app.models import AuditEntry, LoginToken, Membership
from app.security.auth import hash_token
from app.tenancy import Role, system_context
from tests.conftest import add_member, complete_mfa, last_link, make_tenant, make_user, sign_in, totp_secret


def test_magic_link_sign_in_for_reviewer_needs_no_mfa(client):
    t, u = make_tenant(), make_user()
    add_member(t, u, Role.client_reviewer)
    sign_in(client, u)
    me = client.get("/api/auth/me").json()
    assert me["fully_authenticated"] and not me["mfa"]["required"]
    assert client.get(f"/api/tenants/{t.id}/members").status_code == 200


def test_unknown_email_gets_same_response_and_no_email(client):
    before = len(emailer.OUTBOX)
    r = client.post("/api/auth/login", json={"email": "nobody-here@example.com"})
    assert r.status_code == 202
    assert r.json()["message"].startswith("If that email has an account")
    assert len(emailer.OUTBOX) == before


def test_magic_link_is_single_use(client):
    u = make_user()
    client.post("/api/auth/login", json={"email": u.email})
    token = last_link(u.email, "/auth/verify")
    assert client.post("/api/auth/verify", json={"token": token}).status_code == 200
    assert client.post("/api/auth/verify", json={"token": token}).status_code == 400


def test_expired_magic_link_is_rejected(client):
    u = make_user()
    client.post("/api/auth/login", json={"email": u.email})
    token = last_link(u.email, "/auth/verify")
    with db.session(system_context()) as s:
        s.execute(text("UPDATE login_tokens SET expires_at = now() - interval '1 minute' WHERE token_hash = :h"),
                  {"h": hash_token(token)})
    assert client.post("/api/auth/verify", json={"token": token}).status_code == 400


def test_login_is_rate_limited_per_user(client):
    u = make_user()
    for _ in range(8):
        client.post("/api/auth/login", json={"email": u.email})
    with db.session(system_context()) as s:
        assert len(s.scalars(select(LoginToken).where(LoginToken.user_id == u.id)).all()) == 5


def test_only_token_hashes_are_stored(client):
    u = make_user()
    client.post("/api/auth/login", json={"email": u.email})
    token = last_link(u.email, "/auth/verify")
    with db.session(system_context()) as s:
        stored = s.scalars(select(LoginToken.token_hash).where(LoginToken.user_id == u.id)).all()
    assert token not in stored and hash_token(token) in stored


def test_client_admin_must_enrol_mfa_but_can_reach_enrolment(client):
    t, u = make_tenant(), make_user()
    add_member(t, u, Role.client_admin)
    client.post("/api/auth/login", json={"email": u.email})
    client.post("/api/auth/verify", json={"token": last_link(u.email, "/auth/verify")})

    blocked = client.get(f"/api/tenants/{t.id}")
    assert blocked.status_code == 403 and blocked.json()["detail"] == "mfa_enrollment_required"
    me = client.get("/api/auth/me").json()          # the gate's way out stays open
    assert me["mfa"] == {"required": True, "enrolled": False, "verified": False}
    complete_mfa(client, me)
    assert client.get(f"/api/tenants/{t.id}").status_code == 200


def test_enrolled_admin_must_pass_mfa_on_each_new_session(client):
    t, u = make_tenant(), make_user()
    add_member(t, u, Role.client_admin)
    sign_in(client, u)                               # enrols
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"email": u.email})
    client.post("/api/auth/verify", json={"token": last_link(u.email, "/auth/verify")})
    r = client.get(f"/api/tenants/{t.id}")
    assert r.status_code == 403 and r.json()["detail"] == "mfa_required"
    assert client.post("/api/auth/mfa/verify",
                       json={"code": pyotp.TOTP(totp_secret(u.email)).now()}).status_code == 200
    assert client.get(f"/api/tenants/{t.id}").status_code == 200


def test_wrong_mfa_codes_revoke_the_session(client):
    t, u = make_tenant(), make_user()
    add_member(t, u, Role.client_admin)
    sign_in(client, u)
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"email": u.email})
    client.post("/api/auth/verify", json={"token": last_link(u.email, "/auth/verify")})
    for _ in range(5):
        r = client.post("/api/auth/mfa/verify", json={"code": "000000"})
        assert r.status_code == 400
    assert "Too many" in r.json()["detail"]
    assert client.get("/api/auth/me").status_code == 401


def test_promoting_a_reviewer_demands_mfa_from_next_request(client):
    t, u = make_tenant(), make_user()
    add_member(t, u, Role.client_reviewer)
    sign_in(client, u)
    assert client.get(f"/api/tenants/{t.id}").status_code == 200
    with db.session(system_context()) as s:
        s.execute(text("UPDATE memberships SET role='client_admin' WHERE user_id=:u"), {"u": u.id})
    assert client.get(f"/api/tenants/{t.id}").json()["detail"] == "mfa_enrollment_required"


def test_state_changing_requests_need_csrf_header(client):
    del client.headers["x-ppdf-csrf"]
    assert client.post("/api/auth/login", json={"email": "a@example.com"}).status_code == 403


def test_logout_revokes_session(client):
    u = make_user()
    sign_in(client, u)
    assert client.get("/api/auth/me").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


# ------------------------------------------------------------------ invites + roles


def _admin_client(client, tenant):
    admin = make_user()
    add_member(tenant, admin, Role.client_admin)
    sign_in(client, admin)
    return admin


def test_invite_flow_creates_user_and_membership(client):
    t = make_tenant("Invite Co")
    _admin_client(client, t)
    email = f"newbie-{t.slug}@example.com"
    assert client.post(f"/api/tenants/{t.id}/invites",
                       json={"email": email, "role": "client_reviewer"}).status_code == 201
    token = last_link(email, "/invite")

    invitee = type(client)(client.app)
    invitee.headers["x-ppdf-csrf"] = "1"
    preview = invitee.get("/api/auth/invites/preview", params={"token": token}).json()
    assert preview == {"tenant_name": "Invite Co", "email": email, "role": "client_reviewer"}
    assert invitee.post("/api/auth/invites/accept", json={"token": token}).status_code == 200
    me = invitee.get("/api/auth/me").json()
    assert [x["id"] for x in me["tenants"]] == [str(t.id)]
    assert invitee.post("/api/auth/invites/accept", json={"token": token}).status_code == 400

    with db.session(system_context()) as s:
        actions = s.scalars(select(AuditEntry.action).where(AuditEntry.tenant_id == t.id)).all()
    assert {"team.invite_sent", "team.invite_accepted"} <= set(actions)


def test_reviewer_cannot_invite_or_change_roles(client):
    t, reviewer, other = make_tenant(), make_user(), make_user()
    add_member(t, reviewer, Role.client_reviewer)
    add_member(t, other, Role.client_reviewer)
    sign_in(client, reviewer)
    assert client.post(f"/api/tenants/{t.id}/invites",
                       json={"email": "z@example.com", "role": "client_admin"}).status_code == 403
    assert client.patch(f"/api/tenants/{t.id}/members/{other.id}",
                        json={"role": "client_admin"}).status_code == 403
    assert client.get(f"/api/tenants/{t.id}/audit").status_code == 403


def test_last_admin_cannot_be_demoted_or_removed(client):
    t = make_tenant()
    admin = _admin_client(client, t)
    assert client.patch(f"/api/tenants/{t.id}/members/{admin.id}",
                        json={"role": "client_reviewer"}).status_code == 409
    assert client.delete(f"/api/tenants/{t.id}/members/{admin.id}").status_code == 409


def test_role_change_is_audited(client):
    t = make_tenant()
    _admin_client(client, t)
    member = make_user()
    add_member(t, member, Role.client_reviewer)
    assert client.patch(f"/api/tenants/{t.id}/members/{member.id}",
                        json={"role": "client_admin"}).status_code == 200
    entries = client.get(f"/api/tenants/{t.id}/audit").json()["entries"]
    change = next(e for e in entries if e["action"] == "team.role_changed")
    assert change["before"] == {"role": "client_reviewer"} and change["after"] == {"role": "client_admin"}


def test_suspended_tenant_blocks_members(client):
    t, u = make_tenant(), make_user()
    add_member(t, u, Role.client_reviewer)
    sign_in(client, u)
    with db.session(system_context()) as s:
        s.execute(text("UPDATE tenants SET status='suspended' WHERE id=:t"), {"t": t.id})
    assert client.get(f"/api/tenants/{t.id}").status_code == 403


def test_demo_account_signs_in_without_a_code(client):
    r = client.post("/api/auth/demo")
    assert r.status_code == 200
    me = client.get("/api/auth/me").json()
    assert me["user"]["email"] == "demo@publishpdf.ai"
    assert me["fully_authenticated"]
    assert me["tenants"]
    assert not me["user"]["is_platform_admin"]
    client.post("/api/auth/logout")

    with db.session(system_context()) as s:
        s.execute(text("UPDATE users SET is_platform_admin = true WHERE email = 'demo@publishpdf.ai'"))
    refused = client.post("/api/auth/demo")
    assert refused.status_code == 400
    with db.session(system_context()) as s:
        s.execute(text("UPDATE users SET is_platform_admin = false WHERE email = 'demo@publishpdf.ai'"))


def test_admin_creates_tenant_with_first_admin_invite(client):
    sign_in(client, make_user(platform_admin=True))
    slug = f"acme-{make_tenant().slug[-8:]}"
    r = client.post("/api/admin/tenants", json={"name": "Acme", "slug": slug,
                                                "first_admin_email": f"ceo@{slug}.example.com"})
    assert r.status_code == 201
    assert last_link(f"ceo@{slug}.example.com", "/invite")
    assert client.post("/api/admin/tenants", json={"name": "Dup", "slug": slug}).status_code == 409


def test_database_outage_is_a_plain_503_without_the_host():
    from sqlalchemy.exc import OperationalError
    from app.main import DB_DOWN, _db_unreachable
    err = OperationalError("SELECT 1", {}, Exception('connection to server at "10.0.0.1", port 40190 failed'))
    assert _db_unreachable(err) and "10.0.0.1" not in DB_DOWN
