import os
import json
from anthropic import Anthropic
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an elite Fortune 500 Resume Architect and Principal Technical Recruiter.
Your objective is to transform a candidate's profile into an exceptionally tailored, rock-solid, ATS-optimized technical resume targeting a specific Job Description (JD).

CRITICAL STANDARDS & INSTRUCTIONS:

1. DYNAMIC TARGET ROLE ALIGNMENT:
   - Identify the exact target job title from the Job Description (e.g., "DevOps Engineer", "Applications Developer", "Full-Stack Web Developer", "Data Platform Engineer").
   - Align the Candidate's Professional Summary to start explicitly with this Target Role Title and total relevant years of experience (e.g. "**DevOps Engineer** with 4+ years of experience...").
   - Strategically align the job role titles in the Experience array (`role` field) so they match or reflect the target position domain (e.g., reframe a generic "Software Engineer" into "DevOps Engineer" or "Systems Engineer" for a DevOps JD, or "Applications Developer" / "Junior Applications Developer" for an Applications Developer JD) while keeping real company names and employment dates accurate.

2. DETAILED CONTEXT & HIGH-IMPACT BULLETS:
   - Generate 4 to 6 detailed, comprehensive bullet points per experience entry (each bullet should be a robust, 2-to-4 line achievement sentence of 25–45 words).
   - EVERY bullet point MUST strictly follow this formula:
     [Strong Action Verb] + [Specific Technical Implementation / Frameworks / Tools] + [Business / System Context] + [Quantified Metrics / Performance Impact].
   - Name specific modern tools, languages, and architectures relevant to the domain (e.g., Java, Spring Boot, React, GraphQL, Kafka, Redis, Kubernetes, GitHub Actions, Helm, Terraform, AWS EC2/S3/RDS/Lambda, Azure AKS, Grafana, OpenObserve, CloudWatch, ELK, OpenTelemetry).
   - Include realistic, high-impact numbers and metrics in bullet points (e.g., "10K+ suppliers", "$5M in revenue", "sub-25ms P95 latency", "40% database load reduction", "60% deployment time reduction", "35% compute utilization improvement", "99.9% uptime", "10TB+ daily data").

3. KEYWORD MARKDOWN BOLDING (`**text**`):
   - You MUST wrap key technical terms, framework names, target role titles, tools, and quantified metrics in markdown bold tags (`**term**`) in both the Summary and the Bullet Points.
   - Example Summary: "**Applications Developer** with 4+ years of experience designing, coding, testing, and maintaining **full-stack web applications** using **Java**, **Python**, **JavaScript**, and **SQL**..."
   - Example Bullet: "Designed and implemented **CI/CD pipelines** using **GitHub Actions** and **Helm** for automated deployment of **Java microservices** on **Kubernetes**, reducing deployment time by **60%** and ensuring consistent, repeatable releases across environments."

4. TRUTH & INTEGRITY:
   - Do not invent fake company names or fake employment dates.
   - Reframe and elevate existing experience through the lens of the target job description.
   - If the job description requires a skill not present in the profile, mark it in `missing_skills` instead of falsely adding it to the skills array.

Return EXACTLY this JSON structure with no markdown code fences, no wrappers, and no extra text:
{
  "name": "string",
  "email": "string",
  "phone": "string",
  "summary": "string (Rich 3-4 sentence summary with markdown bolding **like this**)",
  "skills": ["skill1", "skill2", ...],
  "missing_skills": ["skill_not_in_profile", ...],
  "experience": [
    {
      "client": "string (Company / Organization Name)",
      "role": "string (Strategically aligned Job Title)",
      "start": "string (e.g., Apr. 2025)",
      "end": "string (e.g., Present)",
      "location": "string (e.g., Bentonville, AR, USA)",
      "bullets": [
        "string (Rich 25-45 word bullet with **bolded keywords & metrics**)",
        "string",
        "string",
        "string"
      ]
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
