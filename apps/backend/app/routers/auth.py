"""Login and session endpoints for the single-user web application."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app import auth

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/login")
async def login(request: Request):
    try:
        payload = await request.json()
        password = payload.get("password") if isinstance(payload, dict) else None
    except Exception:
        password = None
    if not isinstance(password, str) or not password or len(password) > 1024:
        return JSONResponse({"detail": "Login failed."}, status_code=401)
    return auth.issue_login(request, password)


@router.get("/session")
async def session(request: Request) -> dict[str, bool]:
    return {
        "authenticated": auth.app_environment() == "development"
        or auth.authenticated(request)
    }


@router.post("/logout")
async def logout(request: Request):
    return auth.logout(request)
