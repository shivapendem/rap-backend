from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, JSON
from sqlalchemy.orm import relationship
from database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String, nullable=False)  # ADMIN, RECRUITER, CONSULTANT
    name = Column(String, nullable=False)

class Consultant(Base):
    __tablename__ = "consultants"
    
    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    title = Column(String, nullable=False)

class Requirement(Base):
    __tablename__ = "requirements"
    
    id = Column(String, primary_key=True, index=True)
    role = Column(String, nullable=False)
    vendor = Column(String, nullable=False)
    client = Column(String, nullable=False)
    location = Column(String, nullable=False)
    employment_types = Column(JSON, nullable=False)
    work_mode = Column(String, nullable=False)
    received_date = Column(String, nullable=False)
    status = Column(String, nullable=False)
    
    # Store parsed_fields as JSON and vendor_contact as JSON
    parsed_fields = Column(JSON, nullable=True)
    vendor_contact = Column(JSON, nullable=True)
    
    job_description = Column(String, nullable=True)
    
    # Admin fields from requirements/index.ts
    raw_email_id = Column(String, nullable=True)
    rate = Column(String, nullable=True)
    ats_match_count = Column(Integer, nullable=True)
    parse_confidence = Column(Float, nullable=True)
