from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from app.core.limiter import limiter
from app.core.database import SessionLocal
from app.models.book import Coupon, CouponClaim

# Auth is applied globally in main.py via Depends(verify_admin) on include_router.
# Do NOT add a second auth dependency here — it causes double-checking and
# inconsistent error codes (401 vs 403) depending on which guard fires first.
router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
)



class CouponCreate(BaseModel):
    code: str
    discount_type: str
    discount_value: int
    plan_target: str = "standard"
    max_uses: int = 1
    expiry_date: datetime | None = None


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/dashboard")
@limiter.limit("60/minute")
def dashboard(request: Request):
    return {
        "system_status": "healthy",
        "pending_reviews": 12,
        "quarantined_books": 3,
        "approved_today": 42,
        "active_users": 1280,
    }


@router.post("/coupons")
@limiter.limit("20/minute")
def create_coupon(payload: CouponCreate, request: Request):
    if payload.discount_type not in {"percent", "fixed_amount", "free_days"}:
        raise HTTPException(status_code=400, detail="Invalid coupon type.")

    if payload.plan_target != "standard":
        raise HTTPException(status_code=400, detail="Invalid plan target.")

    code = payload.code.strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="Coupon code is required.")

    with SessionLocal() as db:
        if db.query(Coupon).filter(Coupon.code == code).first():
            raise HTTPException(status_code=400, detail="Coupon already exists.")

        coupon = Coupon(
            code=code,
            discount_type=payload.discount_type,
            discount_value=payload.discount_value,
            plan_target=payload.plan_target,
            max_uses=payload.max_uses,
            expiry_date=payload.expiry_date,
        )
        db.add(coupon)
        db.commit()
        db.refresh(coupon)

        return {
            "id": coupon.id,
            "code": coupon.code,
            "discount_type": coupon.discount_type,
            "discount_value": coupon.discount_value,
            "max_uses": coupon.max_uses,
            "used_count": coupon.used_count,
            "expiry_date": coupon.expiry_date,
        }


@router.get("/coupons")
@limiter.limit("60/minute")
def list_coupons(request: Request):
    with SessionLocal() as db:
        coupons = db.query(Coupon).order_by(Coupon.id.desc()).limit(100).all()
        return [
            {
                "id": coupon.id,
                "code": coupon.code,
                "discount_type": coupon.discount_type,
                "discount_value": coupon.discount_value,
                "plan_target": coupon.plan_target,
                "max_uses": coupon.max_uses,
                "used_count": coupon.used_count,
                "expiry_date": coupon.expiry_date,
            }
            for coupon in coupons
        ]


@router.delete("/coupons/{coupon_id}")
@limiter.limit("30/minute")
def delete_coupon(coupon_id: int, request: Request):
    with SessionLocal() as db:
        coupon = db.query(Coupon).filter(Coupon.id == coupon_id).first()
        if not coupon:
            raise HTTPException(status_code=404, detail="Coupon not found.")

        code = coupon.code
        db.query(CouponClaim).filter(CouponClaim.coupon_id == coupon.id).delete()
        db.delete(coupon)
        db.commit()

        return {
            "status": "deleted",
            "id": coupon_id,
            "code": code,
        }
