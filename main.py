from fastapi import FastAPI, Depends, HTTPException, status, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from passlib.context import CryptContext
import jwt
import datetime
import os
from contextlib import asynccontextmanager

from database import engine, Base, get_db, AsyncSessionLocal
from models import User, Requirement, Consultant

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto",bcrypt__truncate_error=False )
SECRET_KEY = os.getenv("SECRET_KEY", "your-super-secret-key-for-dev")
ALGORITHM = "HS256"

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class LoginResponse(BaseModel):
    role: str
    name: str

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: datetime.timedelta = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.datetime.utcnow() + expires_delta
    else:
        expire = datetime.datetime.utcnow() + datetime.timedelta(hours=24)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Seed DB if empty
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).limit(1))
        if not result.scalars().first():
            print("Seeding database...")
            admin = User(
                email="admin@example.com",
                hashed_password=get_password_hash("password123!"),
                role="ADMIN",
                name="Admin User"
            )
            recruiter = User(
                email="recruiter@example.com",
                hashed_password=get_password_hash("password123!"),
                role="RECRUITER",
                name="Recruiter User"
            )
            session.add_all([admin, recruiter])
            await session.commit()
    
    yield

app = FastAPI(lifespan=lifespan)

# Allow requests from the Next.js frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://your-frontend-domain.vercel.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/auth/login", response_model=LoginResponse)
async def login(request: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == request.email))
    user = result.scalars().first()

    if not user or not verify_password(request.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Generate JWT
    access_token = create_access_token(
        data={"sub": user.email, "role": user.role}
    )

    # Set HTTP-only cookie
    response.set_cookie(
        key="rap_session",
        value=access_token,
        httponly=True,
        max_age=24 * 60 * 60,  # 1 day
        expires=24 * 60 * 60,
        samesite="lax",
        secure=os.getenv("NODE_ENV") == "production"  # True in prod
    )

    # Note: For compatibility with existing frontend, we might also want to set 'session'
    response.set_cookie(
        key="session",
        value=access_token,
        httponly=True,
        max_age=24 * 60 * 60,
        expires=24 * 60 * 60,
        samesite="lax",
        secure=os.getenv("NODE_ENV") == "production"
    )

    return LoginResponse(role=user.role, name=user.name)

import httpx

class GoogleLoginRequest(BaseModel):
    code: str
    redirect_uri: str

@app.post("/auth/google/callback", response_model=LoginResponse)
async def google_login(request: GoogleLoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    client_id = os.getenv("GOOGLE_CLIENT_ID", "YOUR_GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "YOUR_GOOGLE_CLIENT_SECRET")
    
    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        token_res = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": request.code,
                "grant_type": "authorization_code",
                "redirect_uri": request.redirect_uri
            }
        )
        
    if token_res.status_code != 200:
        raise HTTPException(status_code=400, detail="Invalid Google OAuth code")
        
    token_data = token_res.json()
    id_token = token_data.get("id_token")
    if not id_token:
        raise HTTPException(status_code=400, detail="Missing id_token from Google")
        
    # Decode id_token (without verification for simplicity here, as it came directly from Google via https)
    # In production, you should verify the signature with Google's public keys
    decoded = jwt.decode(id_token, options={"verify_signature": False})
    email = decoded.get("email")
    
    if not email:
        raise HTTPException(status_code=400, detail="Google token missing email")

    # Check if user exists in DB
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalars().first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User not registered. Please contact your administrator.",
        )

    # Generate JWT
    access_token = create_access_token(
        data={"sub": user.email, "role": user.role}
    )

    # Set HTTP-only cookie
    response.set_cookie(
        key="rap_session",
        value=access_token,
        httponly=True,
        max_age=24 * 60 * 60,  # 1 day
        expires=24 * 60 * 60,
        samesite="lax",
        secure=os.getenv("NODE_ENV") == "production"  # True in prod
    )
    
    response.set_cookie(
        key="session",
        value=access_token,
        httponly=True,
        max_age=24 * 60 * 60,
        expires=24 * 60 * 60,
        samesite="lax",
        secure=os.getenv("NODE_ENV") == "production"
    )

    return LoginResponse(role=user.role, name=user.name)

from typing import List, Optional

class RequirementResponse(BaseModel):
    model_config = {"from_attributes": True}
    id: str
    role: str
    vendor: str
    client: str
    location: str
    employment_types: List[str]
    work_mode: str
    received_date: str
    status: str
    parsed_fields: Optional[dict] = None
    vendor_contact: Optional[dict] = None
    rate: Optional[str] = None
    ats_match_count: Optional[int] = None
    parse_confidence: Optional[float] = None

class PaginatedRequirements(BaseModel):
    data: List[RequirementResponse]
    total: int
    page: int
    page_size: int
    total_pages: int

@app.get("/api/requirements", response_model=PaginatedRequirements)
async def get_requirements(
    page: int = 1,
    page_size: int = 10,
    status: Optional[str] = None,
    sort_by: Optional[str] = "received_date",
    sort_dir: Optional[str] = "desc",
    db: AsyncSession = Depends(get_db)
):
    from sqlalchemy import func
    
    query = select(Requirement)
    
    if status:
        query = query.where(Requirement.status == status)
        
    # Total count
    count_query = select(func.count()).select_from(query.subquery())
    total_res = await db.execute(count_query)
    total = total_res.scalar_one()
    
    # Sorting
    if sort_dir == "desc":
        query = query.order_by(getattr(Requirement, sort_by, Requirement.received_date).desc())
    else:
        query = query.order_by(getattr(Requirement, sort_by, Requirement.received_date).asc())
        
    # Pagination
    query = query.offset((page - 1) * page_size).limit(page_size)
    
    res = await db.execute(query)
    reqs = res.scalars().all()
    
    import math
    total_pages = math.ceil(total / page_size) if page_size > 0 else 0
    
    return PaginatedRequirements(
        data=reqs,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages
    )

class ConsultantResponse(BaseModel):
    model_config = {"from_attributes": True}
    id: str
    name: str
    email: str
    title: str

@app.get("/api/consultants", response_model=List[ConsultantResponse])
async def get_consultants(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Consultant))
    consultants = result.scalars().all()
    return consultants

@app.get("/health")
async def health_check():
    return {"status": "ok"}

