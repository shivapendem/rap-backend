import os
import json
import logging
import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

SYSTEM_PROMPT = """You are an expert resume writer and career strategist. 
Your task is to take a candidate's structured profile information and a target Job Description (JD), and produce a highly tailored, best-in-class resume JSON.
Do not invent clients, projects, or fake years of experience.
If the job description requires a skill not present in the profile, mark it as missing instead of adding it to the skills array.
Improve wording, reorder relevant skills, and add truthful, impactful bullets (use action verbs, metrics where available) based on the candidate's provided experience. Ensure you vary the phrasing so the resume does not sound exactly the same for every job.

Return exactly this JSON structure with no markdown, no code fences, and no extra text:
{
  "name": "string",
  "email": "string",
  "phone": "string",
  "summary": "string (A compelling summary tailored to the JD)",
  "skills": ["skill1", "skill2", ...],
  "missing_skills": ["skill_not_in_profile", ...],
  "experience": [
    {
      "client": "string",
      "role": "string",
      "start": "string",
      "end": "string",
      "location": "string",
      "bullets": ["bullet1", "bullet2", "bullet3", "bullet4"]
    }
  ],
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