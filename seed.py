import asyncio
import json
from database import engine, Base, AsyncSessionLocal
from models import User, Consultant, Requirement
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

async def seed_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    async with AsyncSessionLocal() as session:
        # Check if already seeded
        from sqlalchemy.future import select
        result = await session.execute(select(User).limit(1))
        if result.scalars().first():
            print("DB already seeded.")
            return

        print("Seeding Users...")
        admin = User(
            email="admin@example.com",
            hashed_password=pwd_context.hash("password123!"),
            role="ADMIN",
            name="Admin User"
        )
        recruiter = User(
            email="recruiter@example.com",
            hashed_password=pwd_context.hash("password123!"),
            role="RECRUITER",
            name="Recruiter User"
        )
        session.add_all([admin, recruiter])

        print("Seeding Consultants...")
        consultants = [
            Consultant(id="consultant-001", name="Alex Johnson", email="alex@rap.io", title="Senior React Developer"),
            Consultant(id="con-002", name="Sam Rivera", email="sam@rap.io", title="Full Stack Engineer"),
        ]
        session.add_all(consultants)

        print("Seeding Requirements...")
        import random
        from datetime import datetime, timedelta
        
        ROLES = ["Senior React Developer", "Full Stack Engineer", "DevOps Engineer", "Data Engineer"]
        CLIENTS = ["FinCorp Global", "HealthTech Solutions", "RetailMax"]
        VENDORS = ["TechStaff Inc.", "Apex Staffing"]
        
        reqs = []
        for i in range(1, 56):
            reqs.append(Requirement(
                id=f"req-{i:03d}",
                role=random.choice(ROLES),
                vendor=random.choice(VENDORS),
                client=random.choice(CLIENTS),
                location="Remote",
                employment_types=["C2C", "W2"],
                work_mode="Remote",
                received_date=(datetime.utcnow() - timedelta(days=i)).isoformat() + "Z",
                status="New",
                parsed_fields={"skills": ["React", "TypeScript"], "experience": "5+ years"},
                vendor_contact={"name": "John Doe", "email": "john@vendor.com", "phone": "555-1234"},
                rate="$100/hr",
                ats_match_count=random.randint(1, 20),
                parse_confidence=0.95
            ))
        session.add_all(reqs)
        
        await session.commit()
        print("Seeding completed successfully!")

if __name__ == "__main__":
    asyncio.run(seed_db())
