import hashlib
import hmac
import json
import secrets
import smtplib
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import requests
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from app.core.auth import create_access_token, get_current_profile
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.limiter import limiter
from app.core.storage import save_local_file, upload_object, using_supabase_storage
from app.models.book import (
    Coupon,
    CouponClaim,
    AuthorEarning,
    AuthorFollow,
    Bookmark,
    Book,
    Draft,
    Download,
    InvestorDeal,
    PartnerDeal,
    PaystackEvent,
    PendingPayment,
    Profile,
    ReadingActivity,
    ReadingProgress,
    ReaderAchievement,
    ReaderEngagement,
    Story,
    UserLibraryItem,
    WithdrawalRequest,
)

router = APIRouter(prefix="/api", tags=["Payments"])
password_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

STANDARD_PLAN = "standard"
TERMS_VERSION = "author-agreement-monetization-2026-07-10"
WEEKLY_PLAN_PRICE_KOBO = 100000
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEAL_DOCUMENT_UPLOAD_DIR = PROJECT_ROOT / "private_uploads" / "deal_documents"
MAX_DEAL_DOCUMENT_BYTES = 10 * 1024 * 1024
ALLOWED_DEAL_DOCUMENT_SUFFIXES = {".doc", ".docx", ".pdf", ".jpg", ".jpeg", ".png"}


class SignupPayload(BaseModel):
    email: str
    password: str | None = None
    username: str | None = None
    referralCode: str | None = None
    fingerprint: str | None = None
    termsAccepted: bool = False


class EmailVerificationPayload(BaseModel):
    email: str


class PasswordChangePayload(BaseModel):
    currentPassword: str | None = None
    newPassword: str | None = None


class PasswordResetRequestPayload(BaseModel):
    email: str


class PasswordResetConfirmPayload(BaseModel):
    token: str
    newPassword: str


class ProfilePreferencesPayload(BaseModel):
    username: str | None = None
    emailNotificationsEnabled: bool | None = None
    authorNotificationsEnabled: bool | None = None


class CheckoutPayload(BaseModel):
    userId: str
    email: str
    planType: str
    couponCode: str | None = None
    paidMonths: int = 1
    fingerprint: str | None = None
    referralCode: str | None = None


class CouponPayload(BaseModel):
    userId: str
    code: str
    planType: str = STANDARD_PLAN
    fingerprint: str | None = None


class DonationPayload(BaseModel):
    amountNaira: int
    email: str | None = None
    userId: str | None = None
    authorId: str | None = None
    storyId: int | None = None


class ReferralPayload(BaseModel):
    userId: str
    referralCode: str | None = None
    fingerprint: str | None = None


class PartnerRegistrationPayload(BaseModel):
    name: str
    email: str
    referralCode: str


class InvestorRegistrationPayload(BaseModel):
    name: str
    email: str
    planType: str
    amountNaira: int


class InvestmentCheckoutPayload(InvestorRegistrationPayload):
    pass


def _safe_upload_filename(filename: str) -> str:
    stem = Path(filename or "document").stem.strip() or "document"
    suffix = Path(filename or "").suffix.lower()
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in stem)
    cleaned = cleaned.strip("-_")[:80] or "document"
    return f"{cleaned}{suffix}"


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


def _normalize_custom_code(code: str | None) -> str:
    normalized = _normalize_code(code).replace(" ", "")
    if not normalized or len(normalized) < 4 or len(normalized) > 24:
        raise HTTPException(
            status_code=400,
            detail="Referral code must be 4 to 24 characters.",
        )
    if not all(ch.isalnum() or ch in {"_", "-"} for ch in normalized):
        raise HTTPException(
            status_code=400,
            detail="Referral code can only use letters, numbers, hyphen, or underscore.",
        )
    return normalized


def _clean_email(value: str | None) -> str:
    return (value or "").strip().lower()


def _clean_username(value: str | None) -> str | None:
    username = (value or "").strip()
    if not username:
        return None
    if len(username) < 3 or len(username) > 24:
        raise HTTPException(status_code=400, detail="Username must be 3 to 24 characters.")
    if not all(ch.isalnum() or ch in {"_", "-"} for ch in username):
        raise HTTPException(
            status_code=400,
            detail="Username can only use letters, numbers, hyphen, or underscore.",
        )
    return username


def _clean_password(value: str | None) -> str:
    password = value or ""
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if len(password) > 128:
        raise HTTPException(status_code=400, detail="Password must be 128 characters or fewer.")
    return password


def _paystack_fee_kobo(amount_kobo: int) -> int:
    """Estimate local Paystack fees so partner/investor ROI uses net receipts."""
    if amount_kobo <= 0:
        return 0
    percentage_fee = int(round(amount_kobo * 0.015))
    flat_fee = 10000 if amount_kobo >= 250000 else 0
    return min(200000, percentage_fee + flat_fee)


def _net_receipt_kobo(amount_kobo: int) -> int:
    return max(0, amount_kobo - _paystack_fee_kobo(amount_kobo))


def _safe_public_profile(profile: Profile) -> dict:
    return {
        "id": profile.id,
        "email": profile.email,
        "email_verified": bool(profile.email_verified),
        "username": profile.username,
        "email_notifications_enabled": profile.email_notifications_enabled != 0,
        "author_notifications_enabled": profile.author_notifications_enabled != 0,
        "current_plan": profile.current_plan,
        "subscription_expiry": profile.subscription_expiry,
        "referral_code": profile.referral_code,
        "terms_accepted_at": profile.terms_accepted_at,
        "terms_version": profile.terms_version,
        "access_token": create_access_token(profile.id),
        "token_type": "bearer",
        "expires_in": settings.access_token_expire_minutes * 60,
    }


def _verification_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return token, digest


def _verification_url(token: str) -> str:
    return _local_http_url(f"{settings.app_base_url}/api/auth/verify-email?token={token}")


def _password_reset_url(token: str) -> str:
    return _local_http_url(f"{settings.app_base_url}/?reset_token={token}")


def _send_email(to_email: str, subject: str, body: str) -> bool:
    if not settings.smtp_host or not settings.smtp_from_email:
        return False

    message = EmailMessage()
    message["From"] = settings.smtp_from_email
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
        return True
    except Exception:
        return False


def _send_verification_email(profile: Profile, token: str) -> bool:
    url = _verification_url(token)
    return _send_email(
        profile.email,
        "Verify your PickBook email",
        (
            f"Hi {profile.username or 'there'},\n\n"
            "Verify your PickBook email address with this link:\n"
            f"{url}\n\n"
            "If you did not create this account, you can ignore this email."
        ),
    )


def _send_password_reset_email(profile: Profile, token: str) -> bool:
    url = _password_reset_url(token)
    return _send_email(
        profile.email,
        "Reset your PickBook password",
        (
            f"Hi {profile.username or 'there'},\n\n"
            "Set a new PickBook password with this link:\n"
            f"{url}\n\n"
            "If you did not request this, you can ignore this email."
        ),
    )


def _set_email_verification(profile: Profile) -> str:
    token, digest = _verification_token()
    profile.email_verified = 0
    profile.email_verification_token = digest
    profile.email_verification_sent_at = _now()
    return token


def _set_password_reset(profile: Profile) -> str:
    token, digest = _verification_token()
    profile.password_reset_token = digest
    profile.password_reset_sent_at = _now()
    return token


def _create_pending_payment(
    db,
    reference: str,
    kind: str,
    amount: int,
    metadata: dict,
    user_id: str | None = None,
    plan: str | None = None,
) -> None:
    pending = PendingPayment(
        reference=reference,
        kind=kind,
        user_id=user_id,
        plan=plan,
        amount=amount,
        metadata_json=json.dumps(metadata, ensure_ascii=False),
    )
    db.add(pending)
    db.commit()


def _load_pending_payment(
    db,
    reference: str,
    expected_kind: str | None = None,
) -> tuple[PendingPayment, dict]:
    pending = db.query(PendingPayment).filter(PendingPayment.reference == reference).first()
    if not pending:
        raise HTTPException(status_code=400, detail="Unknown payment reference.")
    if pending.processed_at:
        raise HTTPException(status_code=409, detail="Payment reference already processed.")
    if expected_kind and pending.kind != expected_kind:
        raise HTTPException(status_code=400, detail="Payment reference type mismatch.")
    try:
        metadata = json.loads(pending.metadata_json or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid stored payment metadata.") from exc
    return pending, metadata


def _mark_pending_processed(db, pending: PendingPayment) -> None:
    pending.processed_at = _now()
    db.add(pending)


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


def _is_partner_code(db, code: str | None) -> bool:
    normalized = _normalize_code(code)
    if not normalized:
        return False
    return bool(db.query(PartnerDeal).filter(PartnerDeal.referral_code == normalized).first())


def _register_investor_deal(
    db,
    name: str,
    email: str,
    plan_type: str,
    amount_naira: int,
) -> InvestorDeal:
    clean_email = _clean_email(email)
    plan = (plan_type or "").strip().lower()
    if not name.strip():
        raise HTTPException(status_code=400, detail="Name is required.")
    if not clean_email or "@" not in clean_email:
        raise HTTPException(status_code=400, detail="A valid email is required.")
    if plan not in {"revenue_share", "subscriber_slots"}:
        raise HTTPException(status_code=400, detail="Choose a valid investment plan.")
    if amount_naira < 2500:
        raise HTTPException(status_code=400, detail="Minimum investment is ₦2,500.")

    existing = db.query(InvestorDeal).filter(InvestorDeal.email == clean_email).first()
    if existing:
        existing.name = name.strip()
        existing.plan_type = plan
        existing.amount = amount_naira * 100
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return existing

    investor = InvestorDeal(
        id=str(uuid.uuid4()),
        name=name.strip(),
        email=clean_email,
        plan_type=plan,
        amount=amount_naira * 100,
    )
    db.add(investor)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Investor email already exists.") from exc
    db.refresh(investor)
    return investor


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
    if email:
        _set_email_verification(profile)
    else:
        profile.email_verified = 1
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
    if months == 0:
        return 0
    return 5 if months == 5 else 1


def _plan_days(paid_months: int) -> int:
    if paid_months == 0:
        return 7
    return 180 if paid_months == 5 else 30


def _expected_payment(
    db,
    user_id: str,
    paid_months: int,
    coupon_code: str | None,
    fingerprint: str | None = None,
) -> tuple[int, Coupon | None, int]:
    days = _plan_days(paid_months)

    if paid_months == 0:
        return WEEKLY_PLAN_PRICE_KOBO, None, days

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
    amount: int,
    customer_code: str | None = None,
) -> Profile:
    if db.query(PaystackEvent).filter(PaystackEvent.reference == reference).first():
        pending = db.query(PendingPayment).filter(PendingPayment.reference == reference).first()
        user_id = pending.user_id if pending else None
        profile = db.query(Profile).filter(Profile.id == user_id).first()
        if not profile:
            raise HTTPException(status_code=400, detail="Profile not found.")
        return profile

    pending, metadata = _load_pending_payment(db, reference, expected_kind="subscription")
    user_id = metadata.get("userId")
    plan = metadata.get("planType")
    paid_months = _paid_months(metadata.get("paidMonths"))
    coupon_code = _normalize_code(metadata.get("couponCode"))
    fingerprint = _normalize_fingerprint(metadata.get("fingerprint"))
    referral_code = _normalize_code(metadata.get("referralCode"))
    partner_code = _normalize_code(metadata.get("partnerCode"))

    if plan != STANDARD_PLAN:
        raise HTTPException(status_code=400, detail="Invalid plan.")

    profile = db.query(Profile).filter(Profile.id == user_id).first()
    if not profile:
        raise HTTPException(status_code=400, detail="Profile not found.")

    expected_amount = pending.amount
    coupon = None
    subscription_days = int(metadata.get("subscriptionDays") or _plan_days(paid_months))
    if coupon_code:
        recalculated_amount, coupon, recalculated_days = _expected_payment(
            db,
            profile.id,
            paid_months,
            coupon_code,
            fingerprint,
        )
        if recalculated_amount != expected_amount:
            raise HTTPException(status_code=400, detail="Stored payment mismatch.")
        subscription_days = recalculated_days

    if amount != expected_amount:
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
            paystack_fee=_paystack_fee_kobo(amount),
            net_amount=_net_receipt_kobo(amount),
            referral_code=referral_code or None,
            partner_code=partner_code or None,
        )
    )
    _mark_pending_processed(db, pending)
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


def _post_author_tip(
    db,
    author_id: str | None,
    story_id: int | None,
    amount_kobo: int,
    reference: str,
) -> None:
    if amount_kobo <= 0:
        return
    resolved_author_id = author_id
    resolved_story_id = story_id
    if resolved_story_id and not resolved_author_id:
        story = db.query(Story).filter(Story.id == resolved_story_id).first()
        if story:
            resolved_author_id = story.author_id
    if not resolved_author_id:
        return
    db.add(
        AuthorEarning(
            author_id=resolved_author_id,
            story_id=resolved_story_id,
            source="tip",
            amount_kobo=amount_kobo,
            note=f"Reader tip/donation from payment {reference}.",
        )
    )


@router.post("/auth/signup")
@limiter.limit("5/minute")
def local_signup(payload: SignupPayload, request: Request, db=Depends(get_db)):
    email = _clean_email(payload.email)
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email is required.")
    password = _clean_password(payload.password)
    if not payload.termsAccepted:
        raise HTTPException(
            status_code=400,
            detail="You must accept the PickBook terms before creating an account.",
        )

    profile_id = str(uuid.uuid4())
    profile = Profile(
        id=profile_id,
        email=email,
        password_hash=password_context.hash(password),
        username=_clean_username(payload.username),
        current_plan="free",
        referral_code=_referral_code(profile_id),
        referred_by=_normalize_code(payload.referralCode) or None,
        signup_fingerprint=_normalize_fingerprint(payload.fingerprint) or None,
        terms_accepted_at=_now(),
        terms_version=TERMS_VERSION,
    )
    verification_token = _set_email_verification(profile)
    db.add(profile)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Email or username already exists.") from exc

    response = _safe_public_profile(profile)
    response["verification_required"] = True
    email_sent = _send_verification_email(profile, verification_token)
    response["verification_email_sent"] = email_sent
    if settings.app_env != "production" and not email_sent:
        response["verification_url"] = _verification_url(verification_token)
    return response


@router.post("/auth/login")
@limiter.limit("5/minute")
def local_login(payload: SignupPayload, request: Request, db=Depends(get_db)):
    profile = db.query(Profile).filter(Profile.email == _clean_email(payload.email)).first()
    if not profile:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    if not profile.password_hash:
        raise HTTPException(
            status_code=400,
            detail="This account needs a password reset before password login is available.",
        )
    if not password_context.verify(payload.password or "", profile.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    return _safe_public_profile(profile)


@router.post("/auth/logout")
def local_logout():
    return {"status": "ok"}


@router.put("/auth/password")
@limiter.limit("5/hour")
def change_password(
    payload: PasswordChangePayload,
    request: Request,
    profile: Profile = Depends(get_current_profile),
    db=Depends(get_db),
):
    managed_profile = db.query(Profile).filter(Profile.id == profile.id).first()
    if not managed_profile:
        raise HTTPException(status_code=404, detail="Profile not found.")
    if not managed_profile.password_hash:
        raise HTTPException(
            status_code=400,
            detail="This account needs a password reset before password changes are available.",
        )
    if not password_context.verify(payload.currentPassword or "", managed_profile.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")

    new_password = _clean_password(payload.newPassword)
    if password_context.verify(new_password, managed_profile.password_hash):
        raise HTTPException(status_code=400, detail="Choose a new password that is different from the current one.")

    managed_profile.password_hash = password_context.hash(new_password)
    db.add(managed_profile)
    db.commit()
    return {"status": "updated"}


@router.post("/auth/password-reset/request")
@limiter.limit("5/hour")
def request_password_reset(
    payload: PasswordResetRequestPayload,
    request: Request,
    db=Depends(get_db),
):
    profile = db.query(Profile).filter(Profile.email == _clean_email(payload.email)).first()
    if not profile:
        return {"status": "ok", "message": "If the account exists, a reset link will be sent."}

    token = _set_password_reset(profile)
    db.add(profile)
    db.commit()
    email_sent = _send_password_reset_email(profile, token)
    response = {
        "status": "ok",
        "message": "If the account exists, a reset link will be sent.",
        "email_sent": email_sent,
    }
    if settings.app_env != "production" and not email_sent:
        response["reset_url"] = _password_reset_url(token)
    return response


@router.post("/auth/password-reset/confirm")
@limiter.limit("10/hour")
def confirm_password_reset(
    payload: PasswordResetConfirmPayload,
    request: Request,
    db=Depends(get_db),
):
    digest = hashlib.sha256((payload.token or "").encode("utf-8")).hexdigest()
    profile = db.query(Profile).filter(Profile.password_reset_token == digest).first()
    if not profile:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link.")

    sent_at = profile.password_reset_sent_at
    if sent_at and sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    if sent_at and sent_at < _now() - timedelta(hours=2):
        profile.password_reset_token = None
        profile.password_reset_sent_at = None
        db.add(profile)
        db.commit()
        raise HTTPException(status_code=400, detail="Reset link expired. Request a new one.")

    profile.password_hash = password_context.hash(_clean_password(payload.newPassword))
    profile.password_reset_token = None
    profile.password_reset_sent_at = None
    db.add(profile)
    db.commit()
    return _safe_public_profile(profile)


@router.put("/auth/profile")
@limiter.limit("20/hour")
def update_profile_preferences(
    payload: ProfilePreferencesPayload,
    request: Request,
    profile: Profile = Depends(get_current_profile),
    db=Depends(get_db),
):
    managed_profile = db.query(Profile).filter(Profile.id == profile.id).first()
    if not managed_profile:
        raise HTTPException(status_code=404, detail="Profile not found.")

    if payload.username is not None:
        managed_profile.username = _clean_username(payload.username)
    if payload.emailNotificationsEnabled is not None:
        managed_profile.email_notifications_enabled = 1 if payload.emailNotificationsEnabled else 0
    if payload.authorNotificationsEnabled is not None:
        managed_profile.author_notifications_enabled = 1 if payload.authorNotificationsEnabled else 0

    db.add(managed_profile)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Username already exists.") from exc
    db.refresh(managed_profile)
    return _safe_public_profile(managed_profile)


@router.delete("/auth/account")
@limiter.limit("3/hour")
def delete_account(
    request: Request,
    profile: Profile = Depends(get_current_profile),
    db=Depends(get_db),
):
    managed_profile = db.query(Profile).filter(Profile.id == profile.id).first()
    if not managed_profile:
        raise HTTPException(status_code=404, detail="Profile not found.")

    authored_story_ids = [
        row[0]
        for row in db.query(Story.id)
        .filter(Story.author_id == profile.id)
        .all()
    ]
    authored_book_ids = [
        row[0]
        for row in db.query(Story.published_version_id)
        .filter(
            Story.author_id == profile.id,
            Story.published_version_id.isnot(None),
        )
        .all()
        if row[0]
    ]

    db.query(UserLibraryItem).filter(UserLibraryItem.user_id == profile.id).delete(synchronize_session=False)
    db.query(ReadingProgress).filter(ReadingProgress.user_id == profile.id).delete(synchronize_session=False)
    db.query(Bookmark).filter(Bookmark.user_id == profile.id).delete(synchronize_session=False)
    db.query(Download).filter(Download.user_id == profile.id).delete(synchronize_session=False)
    db.query(ReadingActivity).filter(ReadingActivity.user_id == profile.id).delete(synchronize_session=False)
    db.query(CouponClaim).filter(CouponClaim.user_id == profile.id).delete(synchronize_session=False)
    db.query(PaystackEvent).filter(PaystackEvent.user_id == profile.id).delete(synchronize_session=False)
    db.query(PendingPayment).filter(PendingPayment.user_id == profile.id).delete(synchronize_session=False)
    db.query(ReaderAchievement).filter(ReaderAchievement.user_id == profile.id).delete(synchronize_session=False)
    db.query(ReaderEngagement).filter(ReaderEngagement.user_id == profile.id).delete(synchronize_session=False)
    db.query(AuthorFollow).filter(
        (AuthorFollow.reader_id == profile.id) | (AuthorFollow.author_id == profile.id)
    ).delete(synchronize_session=False)
    db.query(AuthorEarning).filter(AuthorEarning.author_id == profile.id).delete(synchronize_session=False)
    db.query(WithdrawalRequest).filter(WithdrawalRequest.author_id == profile.id).delete(synchronize_session=False)

    if authored_story_ids:
        db.query(UserLibraryItem).filter(UserLibraryItem.story_id.in_(authored_story_ids)).delete(synchronize_session=False)
        db.query(ReadingProgress).filter(ReadingProgress.story_id.in_(authored_story_ids)).delete(synchronize_session=False)
        db.query(Bookmark).filter(Bookmark.story_id.in_(authored_story_ids)).delete(synchronize_session=False)
        db.query(Download).filter(Download.story_id.in_(authored_story_ids)).delete(synchronize_session=False)
        db.query(ReaderEngagement).filter(ReaderEngagement.story_id.in_(authored_story_ids)).delete(synchronize_session=False)
        db.query(AuthorEarning).filter(AuthorEarning.story_id.in_(authored_story_ids)).delete(synchronize_session=False)
        db.query(Draft).filter(Draft.story_id.in_(authored_story_ids)).delete(synchronize_session=False)
        db.query(Story).filter(Story.id.in_(authored_story_ids)).delete(synchronize_session=False)

    if authored_book_ids:
        db.query(UserLibraryItem).filter(UserLibraryItem.book_id.in_(authored_book_ids)).delete(synchronize_session=False)
        db.query(ReadingProgress).filter(ReadingProgress.book_id.in_(authored_book_ids)).delete(synchronize_session=False)
        db.query(Bookmark).filter(Bookmark.book_id.in_(authored_book_ids)).delete(synchronize_session=False)
        db.query(Download).filter(Download.book_id.in_(authored_book_ids)).delete(synchronize_session=False)
        db.query(ReaderEngagement).filter(ReaderEngagement.book_id.in_(authored_book_ids)).delete(synchronize_session=False)
        db.query(Book).filter(Book.id.in_(authored_book_ids)).delete(synchronize_session=False)

    db.delete(managed_profile)
    db.commit()
    return {"status": "deleted"}


@router.post("/auth/request-verification")
@limiter.limit("5/hour")
def request_email_verification(
    payload: EmailVerificationPayload,
    request: Request,
    db=Depends(get_db),
):
    profile = db.query(Profile).filter(Profile.email == _clean_email(payload.email)).first()
    if not profile:
        return {"status": "ok", "message": "If the account exists, a verification link will be sent."}
    if profile.email_verified:
        return {"status": "verified", "message": "Email is already verified."}

    token = _set_email_verification(profile)
    db.add(profile)
    db.commit()
    email_sent = _send_verification_email(profile, token)
    response = {"status": "ok", "message": "Verification link generated.", "email_sent": email_sent}
    if settings.app_env != "production" and not email_sent:
        response["verification_url"] = _verification_url(token)
    return response


@router.get("/auth/verify-email")
@limiter.limit("20/hour")
def verify_email(
    request: Request,
    token: str = Query(..., min_length=20),
    db=Depends(get_db),
):
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    profile = db.query(Profile).filter(Profile.email_verification_token == digest).first()
    if not profile:
        raise HTTPException(status_code=400, detail="Invalid or expired verification link.")

    profile.email_verified = 1
    profile.email_verification_token = None
    profile.email_verification_sent_at = None
    db.add(profile)
    db.commit()
    return {
        "status": "verified",
        "message": "Email verified. You can continue using PickBook.",
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
    referral_code = _normalize_code(payload.referralCode)
    partner_code = referral_code if _is_partner_code(db, referral_code) else None
    amount, coupon, free_days = _coupon_amount(
        db,
        profile.id,
        payload.planType,
        payload.couponCode,
        fingerprint,
    )

    if paid_months != 1 and coupon:
        raise HTTPException(
            status_code=400,
            detail="Promo codes can only be used on the monthly plan.",
        )

    if paid_months == 0:
        amount = WEEKLY_PLAN_PRICE_KOBO

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
        "referralCode": referral_code or None,
        "partnerCode": partner_code or None,
    }
    _create_pending_payment(
        db,
        reference=reference,
        kind="subscription",
        amount=amount,
        metadata=metadata,
        user_id=profile.id,
        plan=STANDARD_PLAN,
    )

    if not settings.paystack_secret_key:
        return {
            "status": "mock",
            "authorization_url": (
                f"{callback_url}"
                f"?reference={reference}&user_id={profile.id}"
                f"&paid_months={paid_months}"
                f"&referral_code={referral_code}"
                f"&partner_code={partner_code or ''}"
            ),
            "reference": reference,
            "amount_kobo": amount,
            "paystack_fee_kobo": _paystack_fee_kobo(amount),
            "net_amount_kobo": _net_receipt_kobo(amount),
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
        "paystack_fee_kobo": _paystack_fee_kobo(amount),
        "net_amount_kobo": _net_receipt_kobo(amount),
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
        "authorId": payload.authorId,
        "storyId": payload.storyId,
    }
    _create_pending_payment(
        db,
        reference=reference,
        kind="donation",
        amount=amount,
        metadata=metadata,
        user_id=profile.id if profile else None,
        plan="donation",
    )

    if not settings.paystack_secret_key:
        return {
            "status": "mock",
            "authorization_url": (
                f"{callback_url}?reference={reference}"
                f"&kind=donation&amount={payload.amountNaira}"
            ),
            "reference": reference,
            "amount_kobo": amount,
            "paystack_fee_kobo": _paystack_fee_kobo(amount),
            "net_amount_kobo": _net_receipt_kobo(amount),
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
        "paystack_fee_kobo": _paystack_fee_kobo(amount),
        "net_amount_kobo": _net_receipt_kobo(amount),
    }


@router.post("/deals/partners/register")
@limiter.limit("6/hour")
def register_partner(
    payload: PartnerRegistrationPayload,
    request: Request,
    db=Depends(get_db),
):
    clean_email = _clean_email(payload.email)
    code = _normalize_custom_code(payload.referralCode)
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="Name is required.")
    if not clean_email or "@" not in clean_email:
        raise HTTPException(status_code=400, detail="A valid email is required.")
    if db.query(Profile).filter(Profile.referral_code == code).first():
        raise HTTPException(
            status_code=400,
            detail="That code is already used by a reader account.",
        )

    existing = db.query(PartnerDeal).filter(PartnerDeal.email == clean_email).first()
    if existing:
        existing.name = payload.name.strip()
        existing.referral_code = code
        db.add(existing)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail="Referral code already exists.") from exc
        db.refresh(existing)
        return {
            "status": "updated",
            "name": existing.name,
            "email": existing.email,
            "referral_code": existing.referral_code,
        }

    partner = PartnerDeal(
        id=str(uuid.uuid4()),
        name=payload.name.strip(),
        email=clean_email,
        referral_code=code,
    )
    db.add(partner)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Email or referral code already exists.") from exc
    db.refresh(partner)
    return {
        "status": "created",
        "name": partner.name,
        "email": partner.email,
        "referral_code": partner.referral_code,
    }


@router.post("/deals/investors/register")
@limiter.limit("6/hour")
def register_investor(
    payload: InvestorRegistrationPayload,
    request: Request,
    db=Depends(get_db),
):
    investor = _register_investor_deal(
        db,
        payload.name,
        payload.email,
        payload.planType,
        payload.amountNaira,
    )
    return {
        "status": investor.status,
        "id": investor.id,
        "name": investor.name,
        "email": investor.email,
        "plan_type": investor.plan_type,
        "amount_kobo": investor.amount,
        "roi_cap_kobo": investor.amount * investor.roi_cap_multiplier,
    }


@router.post("/deals/investors/pay")
@limiter.limit("5/minute")
def initialize_investment(
    payload: InvestmentCheckoutPayload,
    request: Request,
    db=Depends(get_db),
):
    investor = _register_investor_deal(
        db,
        payload.name,
        payload.email,
        payload.planType,
        payload.amountNaira,
    )
    amount = investor.amount
    reference = f"iv_{uuid.uuid4().hex}"
    callback_url = _payment_success_url()
    metadata = {
        "type": "investment",
        "investorId": investor.id,
        "email": investor.email,
        "planType": investor.plan_type,
    }
    _create_pending_payment(
        db,
        reference=reference,
        kind="investment",
        amount=amount,
        metadata=metadata,
        user_id=investor.id,
        plan="investment",
    )

    if not settings.paystack_secret_key:
        return {
            "status": "mock",
            "authorization_url": (
                f"{callback_url}?reference={reference}"
                f"&kind=investment&investor_id={investor.id}"
                f"&amount={int(amount / 100)}"
            ),
            "reference": reference,
            "amount_kobo": amount,
            "paystack_fee_kobo": _paystack_fee_kobo(amount),
            "net_amount_kobo": _net_receipt_kobo(amount),
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
                "email": investor.email,
                "amount": amount,
                "reference": reference,
                "callback_url": callback_url,
                "metadata": metadata,
            },
            timeout=30,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise HTTPException(status_code=400, detail=_paystack_error(exc.response)) from exc
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
        "paystack_fee_kobo": _paystack_fee_kobo(amount),
        "net_amount_kobo": _net_receipt_kobo(amount),
    }


@router.post("/deals/documents/upload")
@limiter.limit("10/hour")
async def upload_deal_document(
    request: Request,
    role: str = Form(...),
    name: str = Form(...),
    email: str = Form(...),
    document: UploadFile = File(...),
):
    normalized_role = role.strip().lower()
    if normalized_role not in {"partner", "investor"}:
        raise HTTPException(status_code=400, detail="Role must be partner or investor.")

    clean_name = name.strip()
    clean_email = _clean_email(email)
    if not clean_name:
        raise HTTPException(status_code=400, detail="Name is required.")
    if not clean_email or "@" not in clean_email:
        raise HTTPException(status_code=400, detail="A valid email is required.")

    suffix = Path(document.filename or "").suffix.lower()
    if suffix not in ALLOWED_DEAL_DOCUMENT_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail="Upload a DOC, DOCX, PDF, JPG, or PNG file.",
        )

    content = await document.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded document is empty.")
    if len(content) > MAX_DEAL_DOCUMENT_BYTES:
        raise HTTPException(status_code=400, detail="Document must be 10 MB or smaller.")

    safe_name = _safe_upload_filename(document.filename or f"{normalized_role}-agreement{suffix}")
    upload_name = f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{normalized_role}_{uuid.uuid4().hex[:10]}_{safe_name}"
    storage_ref = upload_name
    if using_supabase_storage():
        storage_ref = upload_object(
            bucket=settings.supabase_private_bucket,
            object_path=f"deal-documents/{upload_name}",
            content=content,
            content_type=document.content_type or "application/octet-stream",
            public=False,
        )
    else:
        save_local_file(DEAL_DOCUMENT_UPLOAD_DIR, upload_name, content)

    return {
        "status": "received",
        "role": normalized_role,
        "name": clean_name,
        "email": clean_email,
        "filename": safe_name,
        "stored_filename": storage_ref,
        "size_bytes": len(content),
        "message": "Document received. PickBook will review the signed agreement.",
    }


@router.get("/deals/summary")
def deals_summary(
    partner_code: str | None = Query(None),
    investor_email: str | None = Query(None),
    db=Depends(get_db),
):
    code = _normalize_code(partner_code)
    partner = None
    partner_events = []
    if code:
        partner = db.query(PartnerDeal).filter(PartnerDeal.referral_code == code).first()
        partner_events = (
            db.query(PaystackEvent)
            .filter(PaystackEvent.partner_code == code)
            .order_by(PaystackEvent.created_at.desc())
            .all()
        )

    clean_email = _clean_email(investor_email)
    investor = (
        db.query(InvestorDeal).filter(InvestorDeal.email == clean_email).first()
        if clean_email else None
    )
    subscription_events = (
        db.query(PaystackEvent)
        .filter(PaystackEvent.plan == STANDARD_PLAN)
        .order_by(PaystackEvent.created_at.desc())
        .all()
    )

    partner_net = sum(event.net_amount or _net_receipt_kobo(event.amount) for event in partner_events)
    partner_gross = sum(event.amount for event in partner_events)
    partner_fees = sum(event.paystack_fee or _paystack_fee_kobo(event.amount) for event in partner_events)
    all_subscription_net = sum(event.net_amount or _net_receipt_kobo(event.amount) for event in subscription_events)

    investor_summary = None
    if investor:
        cap = investor.amount * investor.roi_cap_multiplier
        projected_due = int(all_subscription_net * 0.15)
        if investor.plan_type == "subscriber_slots":
            projected_due = min(cap, len(subscription_events) * _net_receipt_kobo(settings.standard_plan_price_kobo))
        investor_summary = {
            "name": investor.name,
            "email": investor.email,
            "plan_type": investor.plan_type,
            "status": investor.status,
            "investment_naira": investor.amount / 100,
            "roi_cap_naira": cap / 100,
            "projected_due_naira": min(projected_due, cap) / 100,
            "progress_percent": round((min(projected_due, cap) / cap) * 100, 1) if cap else 0,
        }

    return {
        "charge_note": (
            "Subscription ROI and partner commission are calculated from net receipts: "
            "the customer pays ₦2,500, Paystack/bank charges are removed first, "
            "then earnings are calculated from the balance."
        ),
        "standard_plan": {
            "gross_naira": settings.standard_plan_price_kobo / 100,
            "estimated_paystack_fee_naira": _paystack_fee_kobo(settings.standard_plan_price_kobo) / 100,
            "net_receipt_naira": _net_receipt_kobo(settings.standard_plan_price_kobo) / 100,
        },
        "partner": {
            "found": bool(partner),
            "name": partner.name if partner else None,
            "referral_code": partner.referral_code if partner else code,
            "referred_subscriptions": len(partner_events),
            "gross_revenue_naira": partner_gross / 100,
            "paystack_fees_naira": partner_fees / 100,
            "net_revenue_naira": partner_net / 100,
            "direct_commission_naira": int(partner_net * 0.20) / 100,
            "recent": [
                {
                    "reference": event.reference,
                    "gross_naira": event.amount / 100,
                    "net_naira": (event.net_amount or _net_receipt_kobo(event.amount)) / 100,
                }
                for event in partner_events[:6]
            ],
        },
        "investor": investor_summary,
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
            pending, metadata = _load_pending_payment(db, reference, expected_kind="donation")
            if not db.query(PaystackEvent).filter(PaystackEvent.reference == reference).first():
                db.add(
                    PaystackEvent(
                        reference=reference,
                        user_id=pending.user_id or "anonymous",
                        plan="donation",
                        amount=pending.amount,
                        paystack_fee=_paystack_fee_kobo(pending.amount),
                        net_amount=_net_receipt_kobo(pending.amount),
                    )
                )
                _post_author_tip(
                    db,
                    metadata.get("authorId"),
                    metadata.get("storyId"),
                    _net_receipt_kobo(pending.amount),
                    reference,
                )
            _mark_pending_processed(db, pending)
            db.commit()
            return {
                "status": "success",
                "kind": "donation",
                "amount_naira": int(pending.amount / 100),
            }
        if request.query_params.get("kind") == "investment" or reference.startswith("iv_"):
            pending, metadata = _load_pending_payment(db, reference, expected_kind="investment")
            investor = db.query(InvestorDeal).filter(
                InvestorDeal.id == metadata.get("investorId")
            ).first()
            if investor:
                investor.status = "active"
                db.add(investor)
                if not db.query(PaystackEvent).filter(PaystackEvent.reference == reference).first():
                    db.add(
                        PaystackEvent(
                            reference=reference,
                            user_id=investor.id,
                            plan="investment",
                            amount=pending.amount,
                            paystack_fee=_paystack_fee_kobo(pending.amount),
                            net_amount=_net_receipt_kobo(pending.amount),
                        )
                    )
                _mark_pending_processed(db, pending)
                db.commit()
            return {
                "status": "success",
                "kind": "investment",
                "amount_naira": int(pending.amount / 100),
            }

        if not user_id:
            raise HTTPException(status_code=400, detail="Missing userId.")
        pending, metadata = _load_pending_payment(db, reference, expected_kind="subscription")
        if pending.user_id != user_id:
            raise HTTPException(status_code=403, detail="Payment reference does not belong to this user.")
        profile = _complete_successful_payment(
            db,
            reference,
            pending.amount,
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
        existing = db.query(PaystackEvent).filter(PaystackEvent.reference == reference).first()
        if existing:
            return {
                "status": "success",
                "kind": "donation",
                "amount_naira": int(existing.amount / 100),
            }
        pending, stored_metadata = _load_pending_payment(db, reference, expected_kind="donation")
        amount = int(data.get("amount") or 0)
        if amount != pending.amount:
            raise HTTPException(status_code=400, detail="Amount mismatch.")
        if not db.query(PaystackEvent).filter(PaystackEvent.reference == reference).first():
            db.add(
                PaystackEvent(
                    reference=reference,
                    user_id=pending.user_id or "anonymous",
                    plan="donation",
                    amount=amount,
                    paystack_fee=_paystack_fee_kobo(amount),
                    net_amount=_net_receipt_kobo(amount),
                )
            )
            _post_author_tip(
                db,
                stored_metadata.get("authorId"),
                stored_metadata.get("storyId"),
                _net_receipt_kobo(amount),
                reference,
            )
            _mark_pending_processed(db, pending)
            db.commit()

        return {
            "status": "success",
            "kind": "donation",
            "amount_naira": int(amount / 100),
        }

    if metadata.get("type") == "investment" or reference.startswith("iv_"):
        existing = db.query(PaystackEvent).filter(PaystackEvent.reference == reference).first()
        if existing:
            return {
                "status": "success",
                "kind": "investment",
                "amount_naira": int(existing.amount / 100),
            }
        pending, stored_metadata = _load_pending_payment(db, reference, expected_kind="investment")
        investor = db.query(InvestorDeal).filter(
            InvestorDeal.id == stored_metadata.get("investorId")
        ).first()
        amount = int(data.get("amount") or 0)
        if amount != pending.amount:
            raise HTTPException(status_code=400, detail="Amount mismatch.")
        if investor:
            investor.status = "active"
            db.add(investor)
        if not db.query(PaystackEvent).filter(PaystackEvent.reference == reference).first():
            db.add(
                PaystackEvent(
                    reference=reference,
                    user_id=stored_metadata.get("investorId") or "anonymous",
                    plan="investment",
                    amount=amount,
                    paystack_fee=_paystack_fee_kobo(amount),
                    net_amount=_net_receipt_kobo(amount),
                )
            )
            _mark_pending_processed(db, pending)
        db.commit()
        return {
            "status": "success",
            "kind": "investment",
            "amount_naira": int(amount / 100),
        }

    customer = data.get("customer") or {}
    profile = _complete_successful_payment(
        db,
        reference,
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
        pending, stored_metadata = _load_pending_payment(db, reference, expected_kind="donation")
        if amount != pending.amount:
            raise HTTPException(status_code=400, detail="Amount mismatch.")
        db.add(
            PaystackEvent(
                reference=reference,
                user_id=pending.user_id or "anonymous",
                plan="donation",
                amount=amount,
                paystack_fee=_paystack_fee_kobo(amount),
                net_amount=_net_receipt_kobo(amount),
            )
        )
        _post_author_tip(
            db,
            stored_metadata.get("authorId"),
            stored_metadata.get("storyId"),
            _net_receipt_kobo(amount),
            reference,
        )
        _mark_pending_processed(db, pending)
        db.commit()
        return {"ok": True}

    if metadata.get("type") == "investment" or reference.startswith("iv_"):
        pending, stored_metadata = _load_pending_payment(db, reference, expected_kind="investment")
        if amount != pending.amount:
            raise HTTPException(status_code=400, detail="Amount mismatch.")
        investor = db.query(InvestorDeal).filter(
            InvestorDeal.id == stored_metadata.get("investorId")
        ).first()
        if investor:
            investor.status = "active"
            db.add(investor)
        db.add(
            PaystackEvent(
                reference=reference,
                user_id=stored_metadata.get("investorId") or "anonymous",
                plan="investment",
                amount=amount,
                paystack_fee=_paystack_fee_kobo(amount),
                net_amount=_net_receipt_kobo(amount),
            )
        )
        _mark_pending_processed(db, pending)
        db.commit()
        return {"ok": True}

    if plan != STANDARD_PLAN:
        raise HTTPException(status_code=400, detail="Invalid plan.")

    customer = data.get("customer") or {}
    profile = _complete_successful_payment(
        db,
        reference,
        amount,
        customer_code=customer.get("customer_code"),
    )

    return {"ok": True}


@router.get("/me")
def get_me(profile: Profile = Depends(get_current_profile)):
    response = _safe_public_profile(profile)
    response["streak_count"] = profile.streak_count
    response["last_read_date"] = profile.last_read_date
    return response
