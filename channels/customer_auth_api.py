"""Customer authentication HTTP router."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from agent.customer_identity.service import CustomerIdentityError, CustomerIdentityService
from agent.customer_identity.session_cookie import AUTH_COOKIE, CSRF_COOKIE, new_token


class AuthPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)
    locale: str = "en"


def _raise(exc: CustomerIdentityError):
    raise HTTPException(status_code=exc.status_code, detail=exc.code)


def _anonymous_csrf(request: Request, supplied: str | None) -> None:
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie or not supplied or not secrets.compare_digest(cookie, supplied):
        raise HTTPException(status_code=403, detail="customer_csrf_invalid")


def create_customer_auth_router(service: CustomerIdentityService) -> APIRouter:
    router = APIRouter(prefix="/api/customer/auth", tags=["customer-auth"])

    @router.get("/session")
    async def session(request: Request, response: Response):
        csrf = request.cookies.get(CSRF_COOKIE) or new_token()
        customer = service.resolve(request.cookies.get(AUTH_COOKIE))
        response.set_cookie(
            CSRF_COOKIE, csrf, httponly=False, samesite="lax",
            secure=request.url.scheme == "https",
        )
        if customer is None:
            return {"authenticated": False}
        return {
            "authenticated": True,
            "account_id": customer.account_id,
            "preferred_locale": customer.preferred_locale,
        }

    @router.post("/register", status_code=201)
    async def register(
        payload: AuthPayload, request: Request,
        x_csrf_token: str | None = Header(None),
    ):
        _anonymous_csrf(request, x_csrf_token)
        try:
            account = service.register(payload.email, payload.password, payload.locale)
            return {"account_id": account.account_id, "preferred_locale": account.preferred_locale}
        except CustomerIdentityError as exc:
            _raise(exc)

    @router.post("/login")
    async def login(
        payload: AuthPayload, request: Request, response: Response,
        x_csrf_token: str | None = Header(None),
    ):
        _anonymous_csrf(request, x_csrf_token)
        try:
            old = service.resolve(request.cookies.get(AUTH_COOKIE))
            if old is not None:
                service.logout(old)
            issued = service.login(payload.email, payload.password)
        except CustomerIdentityError as exc:
            _raise(exc)
        response.set_cookie(
            AUTH_COOKIE, issued.token, httponly=True, samesite="lax",
            secure=request.url.scheme == "https", max_age=service.absolute_hours * 3600,
        )
        response.set_cookie(
            CSRF_COOKIE, issued.csrf_token, httponly=False, samesite="lax",
            secure=request.url.scheme == "https", max_age=service.absolute_hours * 3600,
        )
        return {
            "authenticated": True,
            "account_id": issued.customer.account_id,
            "preferred_locale": issued.customer.preferred_locale,
        }

    @router.post("/logout", status_code=204)
    async def logout(
        request: Request, response: Response,
        x_csrf_token: str | None = Header(None),
    ):
        try:
            customer = service.require(request.cookies.get(AUTH_COOKIE))
            service.require_csrf(customer, x_csrf_token)
            service.logout(customer)
        except CustomerIdentityError as exc:
            _raise(exc)
        response.delete_cookie(AUTH_COOKIE)
        response.delete_cookie(CSRF_COOKIE)
        return Response(status_code=204, headers=response.headers)

    return router

