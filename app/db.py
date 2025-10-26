# app/db.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./dev.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args=connect_args,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """Dependency: للحصول على جلسة DB داخل الـ endpoints"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """
    إنشاء الجداول + عمل Seed لعميل تجريبي (Demo) مع مستخدم admin.
    تُستدعَى مرة عند الإقلاع (startup).
    """
    from .models import Tenant, User  # noqa

    # إنشاء الجداول إن لم تكن موجودة
    Base.metadata.create_all(bind=engine)

    # Seed
    from sqlalchemy.orm import Session
   # from passlib.hash import bcrypt  # pip install passlib[bcrypt]
    from passlib.hash import pbkdf2_sha256

    with Session(engine) as db:
        tenant = db.query(Tenant).filter(Tenant.name == "Demo").first()
        if not tenant:
            tenant = Tenant(
                name="Demo",
                k8s_namespace="default",   # مؤقتًا للاختبار
                status="active",
            )
            db.add(tenant)
            db.flush()  # عشان نأخذ tenant.id بعد الإدراج مباشرة

            admin_user = User(
                email="raedbari@lgmail.com",
                password_hash = pbkdf2_sha256.hash("admin123"),  # كلمة مرور بسيطة للاختبار
                role="platform_admin",
                tenant_id=tenant.id,
            )
            db.add(admin_user)
            db.commit()
