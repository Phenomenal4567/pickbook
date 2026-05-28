import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

import requests
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from app.core.auth import get_current_profile
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.limiter import limiter
from app.models.book import Coupon, CouponClaim, PaystackEvent, Profile

router = APIRouter(prefix="/api", tags=["Payments"])

STANDARD_PLAN = "standard"


class SignupPayload(BaseModel):
    email: str


class CheckoutPayload(BaseModel):
    userId: str
    email: str
    planType: str
    couponCode: str | None = None


class CouponPayload(BaseModel):
    userId: str
    code: str
    planType: str = STANDARD_PLAN


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_code(code: str | None) -> str:
    return (code or "").strip().upper()


def _ensure_profile(db, user_id: str, email: str | None = None) -> Profile:
    profile = db.query(Profile).filter(Profile.id == user_id).first()
    if profile:
        return profile

    try:
        uuid.UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid userId.") from exc

    profile = Profile(
        id=user_id,
        email=email or f"{user_id}@local.pickbook",
        current_plan="free",
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _coupon_amount(
    db,
    user_id: str,
    plan_type: str,
    coupon_code: str | None,
) -> tuple[int, Coupon | None, int | None]:
    amount = settings.standard_plan_price_kobo
    code = _normalize_code(coupon_code)

    if plan_type != STANDARD_PLAN:
        raise HTTPException(status_code=400, detail="Invalid plan.")

    if not code:
        return amount, None, None

    coupon = db.query(Coupon).filter(Coupon.code == code).first()
    if not coupon:
        raise HTTPException(status_code=404, detail="Coupon code not found.")

    if coupon.plan_target != plan_type:
        raise HTTPException(status_code=400, detail="Coupon is not valid for this plan.")

    if coupon.used_count >= coupon.max_uses:
        raise HTTPException(status_code=400, detail="Coupon usage limit reached.")

    if coupon.expiry_date:
        expiry = coupon.expiry_date
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry < _now():
            raise HTTPException(status_code=400, detail="Coupon has expired.")

    existing_claim = (
        db.query(CouponClaim)
        .filter(CouponClaim.user_id == user_id, CouponClaim.coupon_id == coupon.id)
        .first()
    )
    if existing_claim:
        raise HTTPException(
            status_code=400,
            detail="You have already used this coupon code.",
        )

    free_days = None
    if coupon.discount_type == "free_days":
        free_days = coupon.discount_value
        amount = 0
    elif coupon.discount_type == "percent":
        amount = int(amount * max(0, 100 - coupon.discount_value) / 100)
        if amount == 0:
            free_days = 30
    elif coupon.discount_type == "fixed_amount":
        amount = max(0, amount - (coupon.discount_value * 100))
        if amount == 0:
            free_days = 30
    else:
        raise HTTPException(status_code=400, detail="Invalid coupon type.")

    return amount, coupon, free_days


def _claim_coupon(db, user_id: str, coupon: Coupon | None) -> None:
    if not coupon:
        return

    coupon.used_count += 1
    db.add(CouponClaim(user_id=user_id, coupon_id=coupon.id))
    db.add(coupon)


def _activate_standard(
    db,
    profile: Profile,
    days: int = 30,
    customer_code: str | None = None,
) -> None:
    start = profile.subscription_expiry or _now()
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if start < _now():
        start = _now()

    profile.current_plan = STANDARD_PLAN
    profile.subscription_expiry = start + timedelta(days=days)
    if customer_code:
        profile.paystack_customer_id = customer_code
    db.add(profile)


@router.post("/auth/signup")
@limiter.limit("5/minute")
def local_signup(payload: SignupPayload, request: Request, db=Depends(get_db)):
    profile_id = str(uuid.uuid4())
    profile = Profile(id=profile_id, email=payload.email, current_plan="free")
    db.add(profile)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Email already exists.") from exc

    return {
        "id": profile.id,
        "email": profile.email,
        "current_plan": profile.current_plan,
        "access_token": profile.id,
    }


@router.post("/auth/login")
@limiter.limit("5/minute")
def local_login(payload: SignupPayload, request: Request, db=Depends(get_db)):
    profile = db.query(Profile).filter(Profile.email == payload.email).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found.")

    return {
        "id": profile.id,
        "email": profile.email,
        "current_plan": profile.current_plan,
        "subscription_expiry": profile.subscription_expiry,
        "access_token": profile.id,
    }


@router.post("/coupon/validate")
@limiter.limit("10/minute")
def validate_coupon(payload: CouponPayload, request: Request, db=Depends(get_db)):
    _ensure_profile(db, payload.userId)
    amount, coupon, free_days = _coupon_amount(
        db,
        payload.userId,
        payload.planType,
        payload.code,
    )

    return {
        "valid": True,
        "code": _normalize_code(payload.code),
        "discount_type": coupon.discount_type if coupon else None,
        "discount_value": coupon.discount_value if coupon else None,
        "final_amount_kobo": amount,
        "free_days": free_days,
    }


@router.post("/checkout/initialize")
@limiter.limit("5/minute")
def initialize_checkout(payload: CheckoutPayload, request: Request, db=Depends(get_db)):
    profile = _ensure_profile(db, payload.userId, payload.email)
    amount, coupon, free_days = _coupon_amount(
        db,
        profile.id,
        payload.planType,
        payload.couponCode,
    )

    if free_days:
        _activate_standard(db, profile, days=free_days)
        _claim_coupon(db, profile.id, coupon)
        db.commit()
        return {
            "status": "activated",
            "authorization_url": None,
            "amount_kobo": 0,
            "message": f"Promo code applied. Standard unlocked for {free_days} days.",
        }

    reference = f"pb_{uuid.uuid4().hex}"
    callback_url = settings.paystack_callback_url or f"{settings.app_base_url}/payment-success"
    metadata = {
        "userId": profile.id,
        "planType": STANDARD_PLAN,
        "couponCode": _normalize_code(payload.couponCode) or None,
    }

    if not settings.paystack_secret_key:
        return {
            "status": "mock",
            "authorization_url": f"{settings.app_base_url}/payment-success?reference={reference}",
            "reference": reference,
            "amount_kobo": amount,
            "metadata": metadata,
        }

    response = requests.post(
        "https://api.paystack.co/transaction/initialize",
        headers={
            "Authorization": f"Bearer {settings.paystack_secret_key}",
            "Content-Type": "application/json",
        },
        json={
            "email": profile.email,
            "amount": amount,
            "reference": reference,
            "callback_url": callback_url,
            "metadata": metadata,
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json().get("data", {})

    return {
        "status": "pending",
        "authorization_url": data.get("authorization_url"),
        "reference": data.get("reference") or reference,
        "amount_kobo": amount,
    }


@router.post("/paystack-webhook")
@limiter.limit("30/minute")
async def paystack_webhook(
    request: Request,
    x_paystack_signature: str = Header(""),
    db=Depends(get_db),
):
    body = await request.body()
    expected = hmac.new(
        settings.paystack_secret_key.encode(),
        body,
        hashlib.sha512,
    ).hexdigest()

    if not settings.paystack_secret_key or not hmac.compare_digest(
        x_paystack_signature,
        expected,
    ):
        raise HTTPException(status_code=400, detail="Invalid signature.")

    event = json.loads(body.decode("utf-8"))
    if event.get("event") != "charge.success":
        return {"ok": True}

    data = event.get("data", {})
    reference = data.get("reference")
    metadata = data.get("metadata") or {}
    user_id = metadata.get("userId")
    plan = metadata.get("planType")
    amount = int(data.get("amount") or 0)

    if not reference:
        raise HTTPException(status_code=400, detail="Missing reference.")

    if db.query(PaystackEvent).filter(PaystackEvent.reference == reference).first():
        return {"ok": True}

    if plan != STANDARD_PLAN:
        raise HTTPException(status_code=400, detail="Invalid plan.")

    if amount < settings.standard_plan_price_kobo:
        raise HTTPException(status_code=400, detail="Amount mismatch.")

    profile = db.query(Profile).filter(Profile.id == user_id).first()
    if not profile:
        raise HTTPException(status_code=400, detail="Profile not found.")

    customer = data.get("customer") or {}
    _activate_standard(db, profile, days=30, customer_code=customer.get("customer_code"))
    db.add(
        PaystackEvent(
            reference=reference,
            user_id=profile.id,
            plan=plan,
            amount=amount,
        )
    )
    db.commit()

    return {"ok": True}


@router.get("/me")
def get_me(profile: Profile = Depends(get_current_profile)):
    return {
        "id": profile.id,
        "email": profile.email,
        "current_plan": profile.current_plan,
        "subscription_expiry": profile.subscription_expiry,
        "streak_count": profile.streak_count,
        "last_read_date": profile.last_read_date,
    }
