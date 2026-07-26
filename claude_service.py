import os
import json
from anthropic import Anthropic
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an elite Fortune 500 Resume Architect and Principal Technical Recruiter.
Your objective is to transform a candidate's profile into an exceptionally tailored, rock-solid, ATS-optimized master technical resume following the comprehensive Shiva Shankar Master Resume Template.

CRITICAL STANDARDS & INSTRUCTIONS:

1. COMPREHENSIVE MASTER RESUME SCHEMA:
   You must populate all relevant standard sections:
   - Header & Contact Info (name, email, phone, location, linkedin, github)
   - CAREER OBJECTIVE / PROFESSIONAL SUMMARY (Rich 3-4 sentence summary tailored to target role with markdown bolding **like this**)
   - TECHNICAL PROFICIENCIES (Categorized array: Programming Languages, DBMS, Frameworks & Tools, Cloud & DevOps, Scripting & Other)
   - EXPERIENCE (Company/Client, Designation/Role, Dates, Location, Description, 4-6 rich bullet points with **bolded keywords & metrics**)
   - KEY PROJECTS (Numbered detailed projects with title, description, role, responsibilities, team_size, duration, technical_tools)
   - ACADEMIC PROJECTS (Academic/university projects with platform, description, role, responsibilities, team_size, duration, technical_tools)
   - OTHER PROJECTS (Additional work, bug fixes, R&D, porting work)
   - EDUCATIONAL BACKGROUND (Degree, institution, year, details/percentage)
   - NON-TECHNICAL PROFICIENCIES (Soft skills, leadership, administration bullets)
   - ACHIEVEMENTS (Certifications, paper presentations, awards)
   - HOBBIES & INTEREST (Interests bullets)
   - PERSONAL DETAILS (Father's name, DOB, languages known, permanent address, desired work location)
   - DECLARATION (Formal declaration text, place, name)

2. DYNAMIC TARGET ROLE ALIGNMENT:
   - Identify the target job title from the Job Description (e.g. "DevOps Engineer", "Applications Developer", "Game Programmer", "Full-Stack Developer").
   - Align the Candidate's Career Objective to start explicitly with this Target Role Title.
   - Strategically align experience role titles (`role`) so they reflect the target domain while keeping authentic company names and dates.

3. DETAILED CONTEXT & HIGH-IMPACT BULLETS:
   - Generate detailed bullet points following: [Action Verb] + [Specific Tech Stack / Frameworks] + [Business / System Context] + [Quantified Impact Metric].
   - Include realistic high-impact metrics (e.g. "10K+ suppliers", "$5M revenue", "sub-25ms latency", "40% database load reduction", "60% deployment reduction", "99.9% uptime").

4. KEYWORD MARKDOWN BOLDING (`**text**`):
   - Wrap key technical terms, framework names, target role titles, tools, and metrics in markdown bold tags (`**term**`).

Return EXACTLY this JSON structure with no markdown code fences:
{
  "name": "string",
  "email": "string",
  "phone": "string",
  "location": "string",
  "linkedin": "string",
  "github": "string",
  "career_objective": "string (Rich tailored objective with **bolded keywords**)",
  "summary": "string",
  "technical_proficiencies": [
    {"category": "Programming Languages", "skills": ["C", "C++", "Java", "Python", "SQL"]},
    {"category": "DBMS", "skills": ["PostgreSQL", "Oracle", "MySQL"]},
    {"category": "Frameworks & Tools", "skills": ["React", "Spring Boot", "Docker", "Kubernetes", "Git"]},
    {"category": "Cloud & DevOps", "skills": ["AWS", "Azure", "CI/CD", "Terraform"]}
  ],
  "skills": ["C", "C++", "Java", "Python", "SQL", "React", "Spring Boot", "AWS", "Docker"],
  "missing_skills": ["skill_not_in_profile"],
  "experience": [
    {
      "client": "string (Company Name)",
      "role": "string (Strategically Aligned Job Title)",
      "start": "string (e.g. Feb 2011)",
      "end": "string (e.g. Present)",
      "location": "string",
      "description": "string (Overview of role & responsibilities)",
      "bullets": [
        "string (Rich 25-45 word bullet with **bolded keywords & metrics**)",
        "string"
      ]
    }
  ],
  "key_projects": [
    {
      "title": "string (Project / Game Title)",
      "description": "string (Project description)",
      "role": "string (Role in project)",
      "responsibilities": ["string", "string"],
      "team_size": "string (e.g. 1)",
      "duration": "string (e.g. 2 Months)",
      "technical_tools": ["tool1", "tool2"]
    }
  ],
  "academic_projects": [
    {
      "title": "string",
      "platform": "string",
      "description": "string",
      "role": "string",
      "responsibilities": ["string"],
      "team_size": "string",
      "duration": "string",
      "technical_tools": ["tool1"]
    }
  ],
  "education": [
    {
      "degree": "string",
      "institution": "string",
      "year": "string",
      "details": "string"
    }
  ],
  "non_technical_proficiencies": ["bullet1", "bullet2"],
  "achievements": ["achievement1", "achievement2"],
  "hobbies_and_interests": ["hobby1", "hobby2"],
  "personal_details": {
    "father_name": "string",
    "dob": "string",
    "marital_status": "string",
    "languages_known": "string",
    "permanent_address": "string",
    "desired_work_location": "string"
  },
  "declaration": {
    "text": "I hereby declare that the above facts given by me are true to the best of my knowledge and belief.",
    "place": "string"
  },
  "generation_notes": "Brief notes on how you tailored the resume."
}"""

def generate_tailored_resume(resume_info: dict, job_description: str) -> tuple[dict, dict]:
    """
    Calls Anthropic API to generate a structured JSON resume based on resume_info and job_description.
    Returns (resume_json, rate_limit_headers).
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    
    mock_fallback = {
        "name": resume_info.get("full_name", "Unknown"),
        "email": resume_info.get("email", ""),
        "phone": resume_info.get("phone", ""),
        "summary": "Highly motivated professional tailored for this role.",
        "skills": resume_info.get("tech_stack", {}).get("expert", []) or ["React", "TypeScript", "Node.js"],
        "missing_skills": [],
        "experience": [
            {
                "client": exp.get("company", "FinCorp Global"),
                "role": exp.get("role", "Software Engineer"),
                "start": exp.get("start_date", "2022-01"),
                "end": exp.get("end_date", "Present"),
                "location": "Remote",
                "bullets": exp.get("bullets", ["Developed responsive web applications", "Integrated REST APIs", "Improved test coverage"])
            }
            for exp in resume_info.get("experience", [])
        ] or [
            {
                "client": "FinCorp Global",
                "role": "Senior Engineer",
                "start": "2022-01",
                "end": "Present",
                "location": "Remote",
                "bullets": ["Developed responsive web applications", "Integrated REST APIs", "Improved test coverage"]
            }
        ],
        "generation_notes": "Mock generated due to missing or invalid Anthropic API key."
    }

    if not api_key or api_key.startswith("your_"):
        logger.warning("ANTHROPIC_API_KEY not found or is a placeholder, returning mock data for testing.")
        return mock_fallback, {}

    try:
        client = Anthropic(api_key=api_key)
        
        user_prompt = f"""
CANDIDATE PROFILE (JSON):
{json.dumps(resume_info, indent=2)}

TARGET JOB DESCRIPTION:
{job_description}

Generate the tailored resume JSON now.
"""
        response = client.messages.with_raw_response.create(
            model="claude-sonnet-4-6",
            max_tokens=2500,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user_prompt}
            ]
        )
        
        # Extract rate limit headers
        headers = response.headers
        rate_limits = {
            "tokens-limit": headers.get("anthropic-ratelimit-tokens-limit"),
            "tokens-remaining": headers.get("anthropic-ratelimit-tokens-remaining"),
            "tokens-reset": headers.get("anthropic-ratelimit-tokens-reset"),
            "requests-limit": headers.get("anthropic-ratelimit-requests-limit"),
            "requests-remaining": headers.get("anthropic-ratelimit-requests-remaining"),
            "requests-reset": headers.get("anthropic-ratelimit-requests-reset")
        }
        
        # Parse content
        parsed_response = response.parse()
        content = parsed_response.content[0].text
        # Sometimes Claude returns wrapped in markdown JSON block
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
            
        result_json = json.loads(content.strip())
        return result_json, rate_limits
    except Exception as e:
        logger.warning(f"Error calling Claude API: {e}. Falling back to mock data.")
        return mock_fallback, {}
