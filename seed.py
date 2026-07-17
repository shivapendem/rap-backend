import asyncio
import random
from datetime import datetime, timedelta, timezone

from passlib.context import CryptContext
from sqlalchemy.future import select
from sqlalchemy.exc import IntegrityError

from database import engine, Base, AsyncSessionLocal
from models import User, Consultant, Requirement

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ---------------------------------------------------------------------------
# Seed data definitions
# ---------------------------------------------------------------------------

SEED_USERS = [
    {
        "full_name": "Admin User",
        "email": "admin@rap.io",
        "password": "Password123!",
        "role": "ADMIN",
    },
    {
        "full_name": "Recruiter User",
        "email": "recruiter@rap.io",
        "password": "Password123!",
        "role": "RECRUITER",
    },
]

SEED_CONSULTANTS = [
    {
        "full_name": "Alex Johnson",
        "email": "alex@rap.io",
        "primary_skills": "React, TypeScript",
        "preferred_roles": "Senior React Developer",
        "status": "ACTIVE",
    },
    {
        "full_name": "Sam Rivera",
        "email": "sam@rap.io",
        "primary_skills": "Node.js, React, PostgreSQL",
        "preferred_roles": "Full Stack Engineer",
        "status": "ACTIVE",
    },
]

ROLES = ["Senior React Developer", "Full Stack Engineer", "DevOps Engineer", "Data Engineer"]
CLIENTS = ["FinCorp Global", "HealthTech Solutions", "RetailMax"]
VENDORS = ["TechStaff Inc.", "Apex Staffing"]
VENDOR_CONTACTS = [
    "John Doe <john@techstaff.com>",
    "Jane Smith <jane@apexstaffing.com>",
]
WORK_MODES = ["Remote", "Hybrid", "Onsite"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _upsert_user(session, data: dict) -> tuple[User, bool]:
    """Insert user if email doesn't exist. Returns (user, created)."""
    result = await session.execute(select(User).where(User.email == data["email"]))
    existing = result.scalars().first()
    if existing:
        return existing, False
    user = User(
        full_name=data["full_name"],
        email=data["email"],
        password_hash=pwd_context.hash(data["password"]),
        role=data["role"],
    )
    session.add(user)
    return user, True


async def _upsert_consultant(session, data: dict) -> tuple[Consultant, bool]:
    """Insert consultant if email doesn't exist. Returns (consultant, created)."""
    result = await session.execute(select(Consultant).where(Consultant.email == data["email"]))
    existing = result.scalars().first()
    if existing:
        return existing, False
    consultant = Consultant(**data)
    session.add(consultant)
    return consultant, True


# ---------------------------------------------------------------------------
# Main seed function
# ---------------------------------------------------------------------------

async def seed_db():
    # Ensure tables exist — safe to call repeatedly (CREATE TABLE IF NOT EXISTS)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:

        # ------------------------------------------------------------------ #
        # Users — insert-if-not-exists keyed by email
        # ------------------------------------------------------------------ #
        created_users, skipped_users = 0, 0
        for data in SEED_USERS:
            _, created = await _upsert_user(session, data)
            if created:
                created_users += 1
            else:
                skipped_users += 1

        await session.flush()

        if created_users:
            print(f"Users: {created_users} inserted, {skipped_users} already existed.")
        else:
            print(f"Users: all {skipped_users} already exist, nothing inserted.")

        # ------------------------------------------------------------------ #
        # Consultants — insert-if-not-exists keyed by email
        # ------------------------------------------------------------------ #
        created_cons, skipped_cons = 0, 0
        for data in SEED_CONSULTANTS:
            _, created = await _upsert_consultant(session, data)
            if created:
                created_cons += 1
            else:
                skipped_cons += 1

        await session.flush()

        if created_cons:
            print(f"Consultants: {created_cons} inserted, {skipped_cons} already existed.")
        else:
            print(f"Consultants: all {skipped_cons} already exist, nothing inserted.")

        # ------------------------------------------------------------------ #
        # Requirements — count existing; only insert the gap up to 55 total
        # so re-running never duplicates rows but will top-up if some were deleted.
        # ------------------------------------------------------------------ #
        from sqlalchemy import func
        count_result = await session.execute(select(func.count()).select_from(Requirement))
        existing_count = count_result.scalar_one()
        target = 0
        to_insert = target - existing_count

        if to_insert > 0:
            reqs = []
            for i in range(1, to_insert + 1):
                reqs.append(
                    Requirement(
                        role=random.choice(ROLES),
                        vendor=random.choice(VENDORS),
                        client=random.choice(CLIENTS),
                        location="Remote, USA",
                        employment_types=["C2C", "W2"],
                        work_mode=random.choice(WORK_MODES),
                        received_date=datetime.now(timezone.utc) - timedelta(days=i),
                        status="NEW",
                        parsed_fields={
                            "skills": ["React", "TypeScript"],
                            "experience": "5+ years",
                        },
                        vendor_contact=random.choice(VENDOR_CONTACTS),
                        rate="$100/hr",
                        ats_match_count=random.randint(1, 20),
                        parse_confidence=round(random.uniform(0.70, 0.99), 2),
                    )
                )
            session.add_all(reqs)
            await session.flush()
            print(f"Requirements: {to_insert} inserted ({existing_count} already existed, target={target}).")
        else:
            print(f"Requirements: {existing_count} already exist (target={target}), nothing inserted.")

        await session.commit()
        print("Seed run complete — no existing data was modified.")


if __name__ == "__main__":
    asyncio.run(seed_db())
