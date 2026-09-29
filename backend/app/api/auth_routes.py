from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import select

from app import db
from app.api.deps import auth_state, client_ip, partial_auth
from app.config import get_settings
from app.models import Membership, Tenant
from app.security import auth
from app.security.auth import AuthError, AuthState

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: EmailStr


class TokenIn(BaseModel):
    token: str


class CodeIn(BaseModel):
    code: str


def _set_session_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(s.session_cookie_name, token, httponly=True, secure=s.cookie_secure,
                        samesite="lax", max_age=s.session_ttl_hours * 3600, path="/")


@router.post("/login", status_code=202)
def login(body: LoginIn, request: Request) -> dict[str, str | None]:
    dev_link = auth.request_magic_link(body.email, client_ip(request))
    out: dict[str, str | None] = {"message": "If that email has an account, a sign-in link is on its way."}
    if dev_link:
        out["dev_link"] = dev_link      # development + console email only (see request_magic_link)
    return out


class CodeRequestIn(BaseModel):
    email: EmailStr


class CodeVerifyIn(BaseModel):
    email: EmailStr
    code: str


class SignupIn(BaseModel):
    signup_token: str
    name: str
    company: str


@router.post("/code", status_code=202)
def request_code(body: CodeRequestIn, request: Request) -> dict:
    try:
        dev_code = auth.request_email_code(body.email, client_ip(request))
    except AuthError as e:
        raise HTTPException(429, str(e)) from e
    out: dict = {"message": f"We sent a 6-digit code to {body.email}."}
    if dev_code:
        out["dev_code"] = dev_code          # development + console email only
    return out


@router.post("/code/verify")
def verify_code(body: CodeVerifyIn, request: Request, response: Response) -> dict:
    try:
        r = auth.verify_email_code(body.email, body.code, client_ip(request), request.headers.get("user-agent"))
    except AuthError as e:
        raise HTTPException(400, str(e)) from e
    if r.session_token:
        _set_session_cookie(response, r.session_token)
        return {"status": "signed_in"}
    return {"status": "needs_profile", "signup_token": r.signup_token}


@router.post("/demo")
def demo_login(request: Request, response: Response) -> dict[str, str]:
    """Public sign-in for the shared demo account. No email code."""
    try:
        token = auth.sign_in_demo(client_ip(request), request.headers.get("user-agent"))
    except AuthError as e:
        status = 429 if str(e).startswith("The demo is busy") else 400
        raise HTTPException(status, str(e)) from e
    _set_session_cookie(response, token)
    return {"status": "signed_in"}


@router.post("/signup")
def signup(body: SignupIn, request: Request, response: Response) -> dict:
    try:
        token = auth.complete_signup(body.signup_token, body.name, body.company, client_ip(request),
                                     request.headers.get("user-agent"))
    except AuthError as e:
        raise HTTPException(400, str(e)) from e
    _set_session_cookie(response, token)
    return {"status": "signed_in"}


@router.post("/verify")
def verify(body: TokenIn, request: Request, response: Response) -> dict[str, str]:
    try:
        token = auth.consume_magic_link(body.token, client_ip(request), request.headers.get("user-agent"))
    except AuthError as e:
        raise HTTPException(400, str(e)) from e
    _set_session_cookie(response, token)
    return {"status": "signed_in"}


@router.get("/me")
def me(state: AuthState = Depends(partial_auth)) -> dict:
    tenants: list[dict] = []
    if state.fully_authenticated:
        with db.session(state.base_context()) as s:
            rows = s.execute(select(Tenant, Membership.role).join(Membership, Membership.tenant_id == Tenant.id)
                             .where(Membership.user_id == state.user.id).order_by(Tenant.name)).all()
            tenants = [{"id": str(t.id), "slug": t.slug, "name": t.name, "status": t.status, "role": role}
                       for t, role in rows]
    return {
        "user": {"id": str(state.user.id), "email": state.user.email, "name": state.user.name,
                 "is_platform_admin": state.user.is_platform_admin},
        "mfa": {"required": state.mfa_required, "enrolled": state.mfa_enrolled,
                "verified": state.mfa_verified},
        "fully_authenticated": state.fully_authenticated,
        "tenants": tenants,
    }


@router.post("/mfa/enroll")
def mfa_enroll(state: AuthState = Depends(partial_auth)) -> dict[str, str]:
    try:
        uri = auth.begin_totp_enrollment(state)
    except AuthError as e:
        raise HTTPException(400, str(e)) from e
    return {"otpauth_uri": uri, "qr_svg": _qr_svg(uri)}


@router.post("/mfa/confirm")
def mfa_confirm(body: CodeIn, state: AuthState = Depends(partial_auth)) -> dict[str, str]:
    try:
        auth.confirm_totp_enrollment(state, body.code)
    except AuthError as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "mfa_enrolled"}


@router.post("/mfa/verify")
def mfa_verify(body: CodeIn, state: AuthState = Depends(partial_auth)) -> dict[str, str]:
    try:
        auth.verify_totp(state, body.code)
    except AuthError as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "mfa_verified"}


@router.post("/logout")
def logout(response: Response, state: AuthState | None = Depends(auth_state)) -> dict[str, str]:
    if state is not None:
        auth.logout(state)
    response.delete_cookie(get_settings().session_cookie_name, path="/")
    return {"status": "signed_out"}


@router.get("/invites/preview")
def invite_preview(token: str) -> dict[str, str]:
    try:
        p = auth.preview_invite(token)
    except AuthError as e:
        raise HTTPException(400, str(e)) from e
    return {"tenant_name": p.tenant_name, "email": p.email, "role": p.role}


@router.post("/invites/accept")
def invite_accept(body: TokenIn, request: Request, response: Response) -> dict[str, str]:
    try:
        token = auth.accept_invite(body.token, client_ip(request), request.headers.get("user-agent"))
    except AuthError as e:
        raise HTTPException(400, str(e)) from e
    _set_session_cookie(response, token)
    return {"status": "signed_in"}


def _qr_svg(data: str) -> str:
    import io

    import qrcode
    import qrcode.image.svg

    buf = io.BytesIO()
    qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8).save(buf)
    return buf.getvalue().decode()
