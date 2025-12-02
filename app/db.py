# app/db.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from .models import ActivityLog  # necessary for table creation

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
    """Dependency: to obtain a DB session inside the endpoints"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """
    Create tables + seed a demo tenant with an admin user.
    Called once during startup.
    """
    from .models import Tenant, User  # noqa

    # Create tables if they don’t exist
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
                k8s_namespace="default",   # temporary for testing
                status="active",
            )
            db.add(tenant)
            db.flush()  # to get tenant.id immediately after insertion

            admin_user = User(
                email="raedbari@lgmail.com",
                password_hash=pbkdf2_sha256.hash("admin123"),  # simple password for testing
                role="platform_admin",
                tenant_id=tenant.id,
            )
            db.add(admin_user)
            db.commit()
