import os
import json
from anthropic import Anthropic
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


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
            model="claude-3-5-sonnet-20240620",
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
