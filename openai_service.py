import os
import json
import logging
import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

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

2. DYNAMIC TARGET ROLE ALIGNMENT (TARGET 80% MATCH):
   - Analyze the provided Job Description to identify the target job title and core required skills.
   - Tailor the Candidate's Career Objective and terminology to closely mirror the Job Description, targeting approximately an 80% match rate with the JD keywords.
   - Strategically align experience role titles (`role`) and bullet points so they reflect the target domain while keeping authentic company names and dates.

3. DETAILED CONTEXT & STRICT TRUTHFULNESS:
   - Generate detailed bullet points following: [Action Verb] + [Specific Tech Stack / Frameworks] + [Business / System Context] + [Quantified Impact Metric].
   - **CRITICAL**: Maintain strict accuracy of the candidate's actual experience. Do NOT invent fake projects, clients, skills, or hallucinate arbitrary metrics. Only include metrics if they are reasonably derived from the provided profile. If a JD skill is entirely absent from the candidate's history, mark it as missing; do not fabricate experience with it. Do NOT add false or unsupported experience simply to force a higher match percentage.

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


async def generate_tailored_resume(resume_info: dict, job_description: str) -> tuple[dict, dict]:
    """
    OpenAI version — same signature/return shape as the old claude_service
    version it replaces, so resume_router.py needs no other changes.
    """
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
        "generation_notes": "Mock generated due to missing or invalid OpenAI API key."
    }

    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY not found, returning mock data for testing.")
        return mock_fallback, {}

    try:
        user_prompt = f"""
CANDIDATE PROFILE (JSON):
{json.dumps(resume_info, indent=2)}

TARGET JOB DESCRIPTION:
{job_description}

Generate the tailored resume JSON now.
"""
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": OPENAI_MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.3,
                    "response_format": {"type": "json_object"},
                },
            )

        if response.status_code != 200:
            logger.warning(f"OpenAI API error {response.status_code}: {response.text[:300]}")
            return mock_fallback, {}

        data = response.json()
        content = data["choices"][0]["message"]["content"]
        result_json = json.loads(content)

        rate_limits = {
            "tokens-limit": response.headers.get("x-ratelimit-limit-tokens"),
            "tokens-remaining": response.headers.get("x-ratelimit-remaining-tokens"),
            "tokens-reset": response.headers.get("x-ratelimit-reset-tokens"),
            "requests-limit": response.headers.get("x-ratelimit-limit-requests"),
            "requests-remaining": response.headers.get("x-ratelimit-remaining-requests"),
            "requests-reset": response.headers.get("x-ratelimit-reset-requests"),
        }
        return result_json, rate_limits
    except Exception as e:
        logger.warning(f"Error calling OpenAI API: {e}. Falling back to mock data.")
        return mock_fallback, {}