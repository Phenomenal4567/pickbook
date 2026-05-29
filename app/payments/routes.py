import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

import requests
from fastapi import APIRouter, Depends, Header, HTTPException, Query
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
    fingerprint: str | None = None


class CheckoutPayload(BaseModel):
    userId: str
    email: str
    planType: str
    couponCode: str | None = None
    paidMonths: int = 1
    fingerprint: str | None = None


class CouponPayload(BaseModel):
    userId: str
    code: str
    planType: str = STANDARD_PLAN
    fingerprint: str | None = None


class DonationPayload(BaseModel):
    amountNaira: int
    email: str | None = None
    userId: str | None = None


class ReferralPayload(BaseModel):
    userId: str
    referralCode: str | None = None
    fingerprint: str | None = None


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


def _normalize_fingerprint(value: str | None) -> str:
    return (value or "").strip()[:128]


def _paystack_error(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}

    return (
        payload.get("message")
        or payload.get("error")
        or "Paystack request failed."
    )


def _local_http_url(url: str) -> str:
    if url.startswith("https://127.0.0.1") or url.startswith("https://localhost"):
        return "http://" + url.removeprefix("https://")
    return url


def _payment_success_url() -> str:
    return _local_http_url(
        settings.paystack_callback_url
        or f"{settings.app_base_url}/payment-success"
    )


def _referral_code(user_id: str) -> str:
    return f"PB{user_id.replace('-', '')[:8].upper()}"


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
        referral_code=_referral_code(user_id),
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _profile_has_real_activity(profile: Profile) -> bool:
    return bool(
        profile.paystack_customer_id
        or profile.subscription_expiry
        or profile.current_plan == STANDARD_PLAN
    )


def _coupon_amount(
    db,
    user_id: str,
    plan_type: str,
    coupon_code: str | None,
    fingerprint: str | None = None,
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

    claim_count_for_coupon = (
        db.query(CouponClaim)
        .filter(CouponClaim.coupon_id == coupon.id)
        .count()
    )
    if claim_count_for_coupon >= coupon.max_uses:
        raise HTTPException(status_code=400, detail="Coupon usage limit reached.")

    fp = _normalize_fingerprint(fingerprint)
    if fp:
        existing_device_claim = (
            db.query(CouponClaim)
            .filter(CouponClaim.coupon_id == coupon.id, CouponClaim.fingerprint == fp)
            .first()
        )
        if existing_device_claim:
            raise HTTPException(
                status_code=400,
                detail="This promo code has already been used on this device.",
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


def _paid_months(value: int | None) -> int:
    try:
        months = int(value or 1)
    except (TypeError, ValueError):
        months = 1
    return 5 if months == 5 else 1


def _plan_days(paid_months: int) -> int:
    return 180 if paid_months == 5 else 30


def _expected_payment(
    db,
    user_id: str,
    paid_months: int,
    coupon_code: str | None,
    fingerprint: str | None = None,
) -> tuple[int, Coupon | None, int]:
    days = _plan_days(paid_months)

    if paid_months > 1:
        return settings.standard_plan_price_kobo * paid_months, None, days

    amount, coupon, free_days = _coupon_amount(
        db,
        user_id,
        STANDARD_PLAN,
        coupon_code,
        fingerprint,
    )

    return amount, coupon, free_days or days


def _complete_successful_payment(
    db,
    reference: str,
    metadata: dict,
    amount: int,
    customer_code: str | None = None,
) -> Profile:
    if db.query(PaystackEvent).filter(PaystackEvent.reference == reference).first():
        user_id = metadata.get("userId")
        profile = db.query(Profile).filter(Profile.id == user_id).first()
        if not profile:
            raise HTTPException(status_code=400, detail="Profile not found.")
        return profile

    user_id = metadata.get("userId")
    plan = metadata.get("planType")
    paid_months = _paid_months(metadata.get("paidMonths"))
    coupon_code = _normalize_code(metadata.get("couponCode"))
    fingerprint = _normalize_fingerprint(metadata.get("fingerprint"))

    if plan != STANDARD_PLAN:
        raise HTTPException(status_code=400, detail="Invalid plan.")

    profile = db.query(Profile).filter(Profile.id == user_id).first()
    if not profile:
        raise HTTPException(status_code=400, detail="Profile not found.")

    expected_amount, coupon, subscription_days = _expected_payment(
        db,
        profile.id,
        paid_months,
        coupon_code,
        fingerprint,
    )

    if amount < expected_amount:
        raise HTTPException(status_code=400, detail="Amount mismatch.")

    _activate_standard(
        db,
        profile,
        days=subscription_days,
        customer_code=customer_code,
    )
    _claim_coupon(db, profile.id, coupon, fingerprint)
    db.add(
        PaystackEvent(
            reference=reference,
            user_id=profile.id,
            plan=plan,
            amount=amount,
        )
    )
    db.commit()
    db.refresh(profile)
    return profile


def _claim_coupon(
    db,
    user_id: str,
    coupon: Coupon | None,
    fingerprint: str | None = None,
) -> None:
    if not coupon:
        return

    coupon.used_count += 1
    db.add(
        CouponClaim(
            user_id=user_id,
            coupon_id=coupon.id,
            fingerprint=_normalize_fingerprint(fingerprint) or None,
        )
    )
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
    profile = Profile(
        id=profile_id,
        email=payload.email,
        current_plan="free",
        referral_code=_referral_code(profile_id),
        signup_fingerprint=_normalize_fingerprint(payload.fingerprint) or None,
    )
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
        "subscription_expiry": profile.subscription_expiry,
        "referral_code": profile.referral_code,
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
        "referral_code": profile.referral_code,
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
        payload.fingerprint,
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
    paid_months = _paid_months(payload.paidMonths)
    fingerprint = _normalize_fingerprint(payload.fingerprint)
    amount, coupon, free_days = _coupon_amount(
        db,
        profile.id,
        payload.planType,
        payload.couponCode,
        fingerprint,
    )

    if paid_months > 1 and coupon:
        raise HTTPException(
            status_code=400,
            detail="Promo codes can only be used on the monthly plan.",
        )

    if paid_months > 1:
        amount = settings.standard_plan_price_kobo * paid_months

    if free_days:
        _activate_standard(db, profile, days=free_days)
        _claim_coupon(db, profile.id, coupon, fingerprint)
        db.commit()
        return {
            "status": "activated",
            "authorization_url": None,
            "amount_kobo": 0,
            "free_days": free_days,
            "message": f"Promo code applied. Standard unlocked for {free_days} days.",
        }

    reference = f"pb_{uuid.uuid4().hex}"
    callback_url = _payment_success_url()
    metadata = {
        "userId": profile.id,
        "planType": STANDARD_PLAN,
        "couponCode": _normalize_code(payload.couponCode) or None,
        "paidMonths": paid_months,
        "subscriptionDays": _plan_days(paid_months),
        "fingerprint": fingerprint,
    }

    if not settings.paystack_secret_key:
        return {
            "status": "mock",
            "authorization_url": (
                f"{callback_url}"
                f"?reference={reference}&user_id={profile.id}"
                f"&paid_months={paid_months}"
            ),
            "reference": reference,
            "amount_kobo": amount,
            "metadata": metadata,
        }

    try:
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
    except requests.HTTPError as exc:
        raise HTTPException(
            status_code=400,
            detail=_paystack_error(exc.response),
        ) from exc
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not connect to Paystack. Please try again.",
        ) from exc

    data = response.json().get("data", {})

    return {
        "status": "pending",
        "authorization_url": data.get("authorization_url"),
        "reference": data.get("reference") or reference,
        "amount_kobo": amount,
    }


@router.post("/donation/initialize")
@limiter.limit("10/minute")
def initialize_donation(
    payload: DonationPayload,
    request: Request,
    db=Depends(get_db),
):
    if payload.amountNaira < 500:
        raise HTTPException(
            status_code=400,
            detail="Minimum donation is ₦500.",
        )

    email = payload.email
    profile = None
    if payload.userId:
        profile = _ensure_profile(db, payload.userId, email)
        email = profile.email

    if not email:
        raise HTTPException(
            status_code=400,
            detail="Email is required for Paystack donation receipts.",
        )

    amount = int(payload.amountNaira * 100)
    reference = f"dn_{uuid.uuid4().hex}"
    callback_url = _payment_success_url()
    metadata = {
        "type": "donation",
        "userId": profile.id if profile else None,
        "amountNaira": payload.amountNaira,
    }

    if not settings.paystack_secret_key:
        return {
            "status": "mock",
            "authorization_url": (
                f"{callback_url}?reference={reference}"
                f"&kind=donation&amount={payload.amountNaira}"
            ),
            "reference": reference,
            "amount_kobo": amount,
            "metadata": metadata,
        }

    try:
        response = requests.post(
            "https://api.paystack.co/transaction/initialize",
            headers={
                "Authorization": f"Bearer {settings.paystack_secret_key}",
                "Content-Type": "application/json",
            },
            json={
                "email": email,
                "amount": amount,
                "reference": reference,
                "callback_url": callback_url,
                "metadata": metadata,
            },
            timeout=30,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise HTTPException(
            status_code=400,
            detail=_paystack_error(exc.response),
        ) from exc
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not connect to Paystack. Please try again.",
        ) from exc

    data = response.json().get("data", {})

    return {
        "status": "pending",
        "authorization_url": data.get("authorization_url"),
        "reference": data.get("reference") or reference,
        "amount_kobo": amount,
    }


@router.post("/referral/claim")
@limiter.limit("6/hour")
def claim_referral_bonus(
    payload: ReferralPayload,
    request: Request,
    db=Depends(get_db),
):
    profile = _ensure_profile(db, payload.userId)
    fingerprint = _normalize_fingerprint(payload.fingerprint)

    if not profile.referral_code:
        profile.referral_code = _referral_code(profile.id)

    if fingerprint and not profile.signup_fingerprint:
        profile.signup_fingerprint = fingerprint

    if profile.referral_bonus_claimed_at:
        return {
            "status": "already_claimed",
            "message": "Your referral boost has already been added.",
            "referral_code": profile.referral_code,
            "subscription_expiry": profile.subscription_expiry,
        }

    code = _normalize_code(payload.referralCode)
    if not code:
        raise HTTPException(
            status_code=400,
            detail="Open a friend referral link before claiming the referral boost.",
        )

    if code == _normalize_code(profile.referral_code):
        raise HTTPException(
            status_code=400,
            detail="You cannot use your own referral code.",
        )

    referrer = (
        db.query(Profile)
        .filter(Profile.referral_code == code)
        .first()
    )
    if not referrer:
        raise HTTPException(
            status_code=404,
            detail="Referral code was not found.",
        )

    if referrer.id == profile.id:
        raise HTTPException(
            status_code=400,
            detail="You cannot refer yourself.",
        )

    if fingerprint and referrer.signup_fingerprint == fingerprint:
        raise HTTPException(
            status_code=400,
            detail="Referral boost is for inviting another reader on a different device.",
        )

    if not _profile_has_real_activity(referrer):
        raise HTTPException(
            status_code=400,
            detail="Referral bonus unlocks after the inviting reader has an active Standard plan or a completed payment.",
        )

    profile.referred_by = code
    profile.referral_bonus_claimed_at = _now()
    _activate_standard(db, profile, days=3)
    db.commit()
    db.refresh(profile)

    return {
        "status": "claimed",
        "message": "Referral boost added: 3 bonus Standard days.",
        "referral_code": profile.referral_code,
        "subscription_expiry": profile.subscription_expiry,
    }


@router.get("/checkout/verify")
@limiter.limit("20/minute")
def verify_checkout(
    request: Request,
    reference: str = Query(...),
    user_id: str | None = Query(None),
    paid_months: int = Query(1),
    db=Depends(get_db),
):
    reference = reference.strip()
    if not reference:
        raise HTTPException(status_code=400, detail="Missing reference.")

    if not settings.paystack_secret_key:
        if request.query_params.get("kind") == "donation" or reference.startswith("dn_"):
            return {
                "status": "success",
                "kind": "donation",
                "amount_naira": int(request.query_params.get("amount") or 0),
            }

        if not user_id:
            raise HTTPException(status_code=400, detail="Missing userId.")
        profile = _ensure_profile(db, user_id)
        metadata = {
            "userId": profile.id,
            "planType": STANDARD_PLAN,
            "paidMonths": _paid_months(paid_months),
            "couponCode": None,
        }
        profile = _complete_successful_payment(
            db,
            reference,
            metadata,
            settings.standard_plan_price_kobo * _paid_months(paid_months),
        )
        return {
            "status": "success",
            "kind": "subscription",
            "current_plan": profile.current_plan,
            "subscription_expiry": profile.subscription_expiry,
        }

    try:
        response = requests.get(
            f"https://api.paystack.co/transaction/verify/{reference}",
            headers={"Authorization": f"Bearer {settings.paystack_secret_key}"},
            timeout=30,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise HTTPException(
            status_code=400,
            detail=_paystack_error(exc.response),
        ) from exc
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not verify payment with Paystack. Please try again.",
        ) from exc
    data = response.json().get("data", {}) or {}

    if data.get("status") != "success":
        raise HTTPException(status_code=400, detail="Payment is not successful yet.")

    metadata = data.get("metadata") or {}
    if metadata.get("type") == "donation" or reference.startswith("dn_"):
        if not db.query(PaystackEvent).filter(PaystackEvent.reference == reference).first():
            db.add(
                PaystackEvent(
                    reference=reference,
                    user_id=metadata.get("userId") or "anonymous",
                    plan="donation",
                    amount=int(data.get("amount") or 0),
                )
            )
            db.commit()

        return {
            "status": "success",
            "kind": "donation",
            "amount_naira": int((data.get("amount") or 0) / 100),
        }

    customer = data.get("customer") or {}
    profile = _complete_successful_payment(
        db,
        reference,
        metadata,
        int(data.get("amount") or 0),
        customer_code=customer.get("customer_code"),
    )

    return {
        "status": "success",
        "kind": "subscription",
        "current_plan": profile.current_plan,
        "subscription_expiry": profile.subscription_expiry,
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

    if metadata.get("type") == "donation" or reference.startswith("dn_"):
        db.add(
            PaystackEvent(
                reference=reference,
                user_id=user_id or "anonymous",
                plan="donation",
                amount=amount,
            )
        )
        db.commit()
        return {"ok": True}

    if plan != STANDARD_PLAN:
        raise HTTPException(status_code=400, detail="Invalid plan.")

    customer = data.get("customer") or {}
    profile = _complete_successful_payment(
        db,
        reference,
        metadata,
        amount,
        customer_code=customer.get("customer_code"),
    )

    return {"ok": True}


@router.get("/me")
def get_me(profile: Profile = Depends(get_current_profile)):
    return {
        "id": profile.id,
        "email": profile.email,
        "current_plan": profile.current_plan,
        "subscription_expiry": profile.subscription_expiry,
        "referral_code": profile.referral_code,
        "streak_count": profile.streak_count,
        "last_read_date": profile.last_read_date,
    }
