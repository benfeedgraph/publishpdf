"""Passwordless auth: 6-digit email codes (and magic links for invites), optional TOTP,
self-serve sign-up, and tenant invites.

Security properties:
- Tokens (magic link, invite, session) are 256-bit random; only their SHA-256 is stored.
- Login never reveals whether an email has an account.
- Magic links and invites are single-use (consumed with a conditional UPDATE).
- MFA is required for platform admins and anyone who is client_admin in any tenant
  (the roles that can publish). It is evaluated on every request, so promoting a
  reviewer to admin demands MFA from their next request on.
- A session that has not passed MFA can still reach the MFA endpoints (enrol /
  verify / logout) — gating the *operation*, never the way out of the gate.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app import audit, db, emailer
from app.config import get_settings
from app.models import AuthSession, Invite, LoginToken, Membership, Tenant, User
from app.tenancy import Context, Role, system_context, user_context

LOGIN_RATE_LIMIT = 5          # magic links per user per window
LOGIN_RATE_WINDOW = timedelta(minutes=15)


class AuthError(Exception):
    """Plain-language auth failure safe to show the user."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _fernet() -> Fernet:
    return Fernet(get_settings().app_secret_key.encode())


def normalize_email(email: str) -> str:
    return email.strip().lower()


# --------------------------------------------------------------------------- login


def request_magic_link(email: str, ip: str | None = None) -> str | None:
    """Email a sign-in link if the account exists. Always returns normally.
    Returns the link only in local development with the console email backend, so
    the dashboard can offer it directly (never in any other environment)."""
    settings = get_settings()
    email = normalize_email(email)
    with db.session(system_context()) as s:
        user = s.scalars(select(User).where(User.email == email)).first()
        if user is None or user.disabled_at is not None:
            return None
        recent = s.scalar(select(func.count()).select_from(LoginToken).where(
            LoginToken.user_id == user.id, LoginToken.created_at > _now() - LOGIN_RATE_WINDOW))
        if recent >= LOGIN_RATE_LIMIT:
            return None
        token = new_token()
        s.add(LoginToken(user_id=user.id, token_hash=hash_token(token),
                         expires_at=_now() + timedelta(minutes=settings.magic_link_ttl_minutes)))
    link = f"{settings.dashboard_base_url}/auth/verify?token={quote(token)}"
    emailer.send_soon(email, "Your sign-in link",
                 f"Click to sign in:\n\n{link}\n\n"
                 f"This link expires in {settings.magic_link_ttl_minutes} minutes and works once.\n"
                 "If you didn't ask for it, you can ignore this email.")
    if settings.env == "development" and settings.email_backend == "console":
        return link
    return None


def consume_magic_link(token: str, ip: str | None, user_agent: str | None) -> str:
    """Exchange a magic-link token for a new session token."""
    with db.session(system_context()) as s:
        user_id = s.execute(text("""
            UPDATE login_tokens SET used_at = now()
            WHERE token_hash = :h AND used_at IS NULL AND expires_at > now()
            RETURNING user_id"""), {"h": hash_token(token)}).scalar()
        if user_id is None:
            raise AuthError("This sign-in link is invalid or has expired. Request a new one.")
        user = s.get(User, user_id)
        if user is None or user.disabled_at is not None:
            raise AuthError("This account is disabled.")
        session_token = _create_session(s, user.id, ip, user_agent)
        audit.record(s, system_context(), "auth.login", target_type="user", target_id=user.id,
                     actor_user_id=user.id, ip=ip)
    return session_token


def _create_session(s: Session, user_id: uuid.UUID, ip: str | None, user_agent: str | None, *,
                    mfa_verified: bool = False) -> str:
    token = new_token()
    s.add(AuthSession(user_id=user_id, token_hash=hash_token(token), ip=ip,
                      user_agent=(user_agent or "")[:500],
                      mfa_verified_at=_now() if mfa_verified else None,
                      expires_at=_now() + timedelta(hours=get_settings().session_ttl_hours)))
    return token


DEMO_EMAIL = "demo@publishpdf.ai"
DEMO_RATE_LIMIT = 30          # sessions for the shared demo account per window


def sign_in_demo(ip: str | None, user_agent: str | None) -> str:
    """Sign in the shared demo account, with no email code.

    Only ``demo@publishpdf.ai``. The account is created on first use, with its own
    workspace, and is never a platform admin. The session is marked past MFA because
    this identity is public: there is no secret to protect, and a 2FA screen would
    stop everyone who clicked the button.
    """
    with db.session(system_context()) as s:
        user = s.scalars(select(User).where(User.email == DEMO_EMAIL)).first()
        if user is None:
            user = User(email=DEMO_EMAIL, name="Demo")
            s.add(user)
            s.flush()
        if user.disabled_at is not None:
            raise AuthError("The demo account is unavailable right now.")
        if user.is_platform_admin:
            raise AuthError("The demo account can't be used this way. Sign in with your email instead.")
        recent = s.scalar(select(func.count()).select_from(AuthSession).where(
            AuthSession.user_id == user.id, AuthSession.created_at > _now() - LOGIN_RATE_WINDOW))
        if recent >= DEMO_RATE_LIMIT:
            raise AuthError("The demo is busy. Wait a few minutes and try again.")
        if s.scalar(select(func.count()).select_from(Membership).where(Membership.user_id == user.id)) == 0:
            tenant = s.scalars(select(Tenant).where(Tenant.slug == "demo")).first()
            if tenant is None:
                tenant = Tenant(slug="demo", name="PublishPDF Demo")
                s.add(tenant)
                s.flush()
            s.add(Membership(tenant_id=tenant.id, user_id=user.id, role=Role.client_admin.value))
        token = _create_session(s, user.id, ip, user_agent, mfa_verified=True)
        audit.record(s, system_context(), "auth.login", target_type="user", target_id=user.id,
                     actor_user_id=user.id, ip=ip, after={"method": "demo"})
    return token


# --------------------------------------------------------------------------- email codes


CODE_RATE_LIMIT = 5            # codes per email per window
CODE_MAX_ATTEMPTS = 5          # wrong guesses before a code is burned


def _code_hash(email: str, code: str) -> str:
    return hashlib.sha256(f"{get_settings().app_secret_key}:{email}:{code}".encode()).hexdigest()


def request_email_code(email: str, ip: str | None = None) -> str | None:
    """Send a 6-digit sign-in code to any address (existing account or not, so the
    response never reveals which). Returns the code only in local development with
    the console email backend."""
    from app.models import EmailCode

    settings = get_settings()
    email = normalize_email(email)
    with db.session(system_context()) as s:
        recent = s.scalar(select(func.count()).select_from(EmailCode).where(
            EmailCode.email == email, EmailCode.created_at > _now() - LOGIN_RATE_WINDOW))
        if recent >= CODE_RATE_LIMIT:
            raise AuthError("Too many codes requested. Wait a few minutes and try again.")
        user = s.scalars(select(User).where(User.email == email)).first()
        if user is not None and user.disabled_at is not None:
            return None
        code = f"{secrets.randbelow(10 ** 6):06d}"
        s.add(EmailCode(email=email, code_hash=_code_hash(email, code), ip=ip,
                        expires_at=_now() + timedelta(minutes=settings.email_code_ttl_minutes)))
    emailer.send_soon(email, f"{code} is your PublishPDF code",
                 f"Your sign-in code is:\n\n    {code}\n\nIt expires in {settings.email_code_ttl_minutes} minutes. "
                 "If you didn't ask for it, you can ignore this email.")
    if settings.env == "development" and settings.email_backend == "console":
        return code
    return None


@dataclass(frozen=True)
class CodeResult:
    session_token: str | None = None      # existing user: signed in
    signup_token: str | None = None       # new email: finish sign-up with a name and company


def verify_email_code(email: str, code: str, ip: str | None, user_agent: str | None) -> CodeResult:
    from app.models import EmailCode, SignupToken

    email = normalize_email(email)
    code = "".join(ch for ch in code if ch.isdigit())
    with db.session(system_context()) as s:
        row = s.scalars(select(EmailCode).where(EmailCode.email == email, EmailCode.used_at.is_(None),
                                                EmailCode.expires_at > func.now())
                        .order_by(EmailCode.created_at.desc())).first()
        if row is None:
            raise AuthError("That code has expired. Request a new one.")
        if row.attempts >= CODE_MAX_ATTEMPTS:
            raise AuthError("Too many wrong codes. Request a new one.")
        if not secrets.compare_digest(row.code_hash, _code_hash(email, code)):
            row.attempts += 1
            s.commit()
            left = CODE_MAX_ATTEMPTS - row.attempts
            raise AuthError("That code isn't right." + (f" {left} tries left." if left > 0 else " Request a new one."))
        row.used_at = _now()
        user = s.scalars(select(User).where(User.email == email)).first()
        if user is not None:
            if user.disabled_at is not None:
                raise AuthError("This account is disabled.")
            token = _create_session(s, user.id, ip, user_agent)
            audit.record(s, system_context(), "auth.login", target_type="user", target_id=user.id,
                         actor_user_id=user.id, ip=ip, after={"method": "email_code"})
            return CodeResult(session_token=token)
        if not get_settings().allow_self_signup:
            raise AuthError("There's no account for this email. Ask your administrator for an invitation.")
        signup = new_token()
        s.add(SignupToken(email=email, token_hash=hash_token(signup), expires_at=_now() + timedelta(minutes=30)))
        return CodeResult(signup_token=signup)


def complete_signup(signup_token: str, name: str, company: str, ip: str | None, user_agent: str | None) -> str:
    """Create the user and their own workspace (they become its admin); returns a session token."""
    import re as _re

    from app.models import SignupToken

    name, company = name.strip()[:120], company.strip()[:200]
    if not name or not company:
        raise AuthError("Enter your name and your company's name.")
    with db.session(system_context()) as s:
        st = s.execute(text("""UPDATE signup_tokens SET used_at = now()
                               WHERE token_hash = :h AND used_at IS NULL AND expires_at > now()
                               RETURNING email"""), {"h": hash_token(signup_token)}).scalar()
        if st is None:
            raise AuthError("This sign-up has expired. Start again with your email.")
        if s.scalars(select(User).where(User.email == st)).first():
            raise AuthError("An account already exists for this email. Sign in instead.")
        user = User(email=st, name=name)
        s.add(user)
        base = _re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")[:40] or "workspace"
        slug, n = base, 1
        while s.scalars(select(Tenant).where(Tenant.slug == slug)).first():
            n += 1
            slug = f"{base}-{n}"
        tenant = Tenant(slug=slug, name=company)
        s.add(tenant)
        s.flush()
        s.add(Membership(tenant_id=tenant.id, user_id=user.id, role=Role.client_admin.value))
        audit.record(s, system_context(), "auth.signup", tenant_id=tenant.id, actor_user_id=user.id,
                     target_type="user", target_id=user.id, after={"email": st, "company": company, "slug": slug}, ip=ip)
        _ = SignupToken
        return _create_session(s, user.id, ip, user_agent)


# --------------------------------------------------------------------------- sessions


@dataclass(frozen=True)
class AuthState:
    user: User
    session_id: uuid.UUID
    mfa_required: bool
    mfa_enrolled: bool
    mfa_verified: bool

    @property
    def fully_authenticated(self) -> bool:
        return self.mfa_verified or not self.mfa_required

    def base_context(self) -> Context:
        return user_context(self.user.id, platform_admin=self.user.is_platform_admin)


def resolve_session(token: str | None) -> AuthState | None:
    if not token:
        return None
    # One query (runs on every request): session + user + "admin anywhere" together.
    is_admin_q = (select(Membership.id).where(Membership.user_id == User.id,
                                              Membership.role == Role.client_admin.value)
                  .exists().label("is_any_admin"))
    with db.session(system_context()) as s:
        row = s.execute(select(AuthSession, User, is_admin_q)
                        .join(User, User.id == AuthSession.user_id)
                        .where(AuthSession.token_hash == hash_token(token))).first()
        if row is None:
            return None
        sess, user, is_any_admin = row
        if sess.revoked_at is not None or sess.expires_at <= _now():
            return None
        if user.disabled_at is not None:
            return None
        s.expunge(user)
        return AuthState(
            user=user,
            session_id=sess.id,
            mfa_required=user.totp_enabled_at is not None
            or (get_settings().mfa_required_for_admins and (user.is_platform_admin or is_any_admin)),
            mfa_enrolled=user.totp_enabled_at is not None,
            mfa_verified=sess.mfa_verified_at is not None,
        )


def logout(state: AuthState) -> None:
    with db.session(state.base_context()) as s:
        s.execute(text("UPDATE sessions SET revoked_at = now() WHERE id = :id"), {"id": state.session_id})


# --------------------------------------------------------------------------- MFA (TOTP)


def begin_totp_enrollment(state: AuthState) -> str:
    """Create (or replace an unconfirmed) TOTP secret; returns the otpauth:// URI."""
    if state.mfa_enrolled:
        raise AuthError("Two-factor authentication is already set up for this account.")
    secret = pyotp.random_base32()
    with db.session(state.base_context()) as s:
        user = s.get(User, state.user.id)
        assert user is not None
        user.totp_secret_enc = _fernet().encrypt(secret.encode()).decode()
    return pyotp.TOTP(secret).provisioning_uri(name=state.user.email,
                                              issuer_name=get_settings().totp_issuer)


def confirm_totp_enrollment(state: AuthState, code: str) -> None:
    if state.mfa_enrolled:
        raise AuthError("Two-factor authentication is already set up for this account.")
    with db.session(state.base_context()) as s:
        user = s.get(User, state.user.id)
        assert user is not None
        if not user.totp_secret_enc:
            raise AuthError("Start two-factor setup first.")
        _check_code(s, state, user, code)
        user.totp_enabled_at = _now()
        s.execute(text("UPDATE sessions SET mfa_verified_at = now() WHERE id = :id"),
                  {"id": state.session_id})
        audit.record(s, state.base_context(), "auth.mfa_enrolled", target_type="user", target_id=user.id)


def verify_totp(state: AuthState, code: str) -> None:
    if not state.mfa_enrolled:
        raise AuthError("Set up two-factor authentication first.")
    with db.session(state.base_context()) as s:
        user = s.get(User, state.user.id)
        assert user is not None
        _check_code(s, state, user, code)
        s.execute(text("UPDATE sessions SET mfa_verified_at = now() WHERE id = :id"),
                  {"id": state.session_id})


def _check_code(s: Session, state: AuthState, user: User, code: str) -> None:
    try:
        secret = _fernet().decrypt((user.totp_secret_enc or "").encode()).decode()
    except InvalidToken as e:
        raise AuthError("Two-factor setup is corrupted; contact support.") from e
    if pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1):
        s.execute(text("UPDATE sessions SET mfa_failed_attempts = 0 WHERE id = :id"),
                  {"id": state.session_id})
        return
    attempts = s.execute(text("""UPDATE sessions SET mfa_failed_attempts = mfa_failed_attempts + 1
                                 WHERE id = :id RETURNING mfa_failed_attempts"""),
                         {"id": state.session_id}).scalar_one()
    if attempts >= get_settings().totp_max_attempts:
        s.execute(text("UPDATE sessions SET revoked_at = now() WHERE id = :id"), {"id": state.session_id})
        s.commit()
        raise AuthError("Too many incorrect codes. Sign in again.")
    s.commit()
    raise AuthError("That code is incorrect. Try again.")


# --------------------------------------------------------------------------- invites


def create_invite(s: Session, ctx: Context, email: str, role: Role) -> Invite:
    """Invite someone into ctx's tenant. Caller must have checked team.manage."""
    settings = get_settings()
    tenant_id = ctx.require_tenant()
    email = normalize_email(email)
    token = new_token()
    invite = Invite(tenant_id=tenant_id, email=email, role=role.value, token_hash=hash_token(token),
                    invited_by=ctx.user_id,
                    expires_at=_now() + timedelta(days=settings.invite_ttl_days))
    s.add(invite)
    s.flush()
    tenant = s.get(Tenant, tenant_id)
    audit.record(s, ctx, "team.invite_sent", target_type="invite", target_id=invite.id,
                 after={"email": email, "role": role.value})
    link = f"{settings.dashboard_base_url}/invite?token={quote(token)}"
    emailer.send(email, f"You're invited to {tenant.name if tenant else 'a workspace'}",
                 f"You've been invited as {role.value.replace('_', ' ')}.\n\nAccept:\n{link}\n\n"
                 f"This invitation expires in {settings.invite_ttl_days} days.")
    return invite


@dataclass(frozen=True)
class InvitePreview:
    tenant_name: str
    email: str
    role: str


def preview_invite(token: str) -> InvitePreview:
    with db.session(system_context()) as s:
        inv = _valid_invite(s, token)
        tenant = s.get(Tenant, inv.tenant_id)
        assert tenant is not None
        return InvitePreview(tenant.name, inv.email, inv.role)


def accept_invite(token: str, ip: str | None, user_agent: str | None) -> str:
    """Accept an invite: create the user if needed, add the membership, start a session.
    The invite link reached the invitee's inbox, so it proves email ownership."""
    with db.session(system_context()) as s:
        inv = _valid_invite(s, token)
        claimed = s.execute(text("""UPDATE invites SET accepted_at = now()
                                    WHERE id = :id AND accepted_at IS NULL RETURNING id"""),
                            {"id": inv.id}).scalar()
        if claimed is None:
            raise AuthError("This invitation has already been used.")
        user = s.scalars(select(User).where(User.email == inv.email)).first()
        if user is None:
            user = User(email=inv.email)
            s.add(user)
            s.flush()
        elif user.disabled_at is not None:
            raise AuthError("This account is disabled.")
        existing = s.scalars(select(Membership).where(Membership.tenant_id == inv.tenant_id,
                                                      Membership.user_id == user.id)).first()
        if existing is None:
            s.add(Membership(tenant_id=inv.tenant_id, user_id=user.id, role=inv.role))
        else:
            existing.role = inv.role
        audit.record(s, system_context(), "team.invite_accepted", tenant_id=inv.tenant_id,
                     actor_user_id=user.id, target_type="user", target_id=user.id,
                     after={"role": inv.role}, ip=ip)
        return _create_session(s, user.id, ip, user_agent)


def _valid_invite(s: Session, token: str) -> Invite:
    inv = s.scalars(select(Invite).where(Invite.token_hash == hash_token(token))).first()
    if inv is None or inv.revoked_at is not None or inv.expires_at <= _now():
        raise AuthError("This invitation is invalid or has expired. Ask your admin for a new one.")
    if inv.accepted_at is not None:
        raise AuthError("This invitation has already been used.")
    tenant = s.get(Tenant, inv.tenant_id)
    if tenant is None or tenant.status != "active":
        raise AuthError("This workspace is not active.")
    return inv
