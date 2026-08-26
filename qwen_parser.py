import os
import json
import logging
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

HF_TOKEN = os.getenv("HF_TOKEN", "")
# HelixCipher/job-posting-extractor-qwen is finetuned on Qwen2.5-3B-Instruct for JSON extraction
MODEL_NAME = "HelixCipher/job-posting-extractor-qwen"

SYSTEM_PROMPT = """You are a JSON extraction assistant. Extract the following fields from the job posting: role, client, location, rate, duration, work_mode, employment_types, experience, skills. Always output ONLY valid JSON matching exactly this schema:
{
  "role": "Job title or null",
  "client": "Company name or null",
  "location": "Location or null",
  "rate": "Rate or null",
  "duration": "Duration or null",
  "work_mode": "REMOTE, HYBRID, ONSITE, or UNKNOWN",
  "employment_types": ["C2C", "W2", "CONTRACT", "FULLTIME"],
  "experience": "Experience required or null",
  "skills": ["Skill1", "Skill2"]
}"""

def parse_requirement_qwen(subject: str, body: str) -> Optional[dict]:
    if not HF_TOKEN:
        logger.warning("HF_TOKEN not found, skipping Qwen Hugging Face parser.")
        return None

    try:
        from openai import OpenAI
        # Use OpenAI client but point to Hugging Face Serverless API!
        client = OpenAI(
            base_url=f"https://api-inference.huggingface.co/models/{MODEL_NAME}/v1",
            api_key=HF_TOKEN
        )
        
        user_prompt = f"SUBJECT: {subject}\n\nBODY: {body}"
        
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            max_tokens=1500
        )
        
        content = response.choices[0].message.content
        parsed = json.loads(content)
        parsed["parsing_model"] = f"Hugging Face ({MODEL_NAME})"
        
        if not parsed.get("work_mode"):
            parsed["work_mode"] = "UNKNOWN"
        if not parsed.get("employment_types"):
            parsed["employment_types"] = ["UNKNOWN"]
            
        return parsed
            
    except Exception as e:
        logger.warning(f"Error calling Hugging Face API for Qwen parser: {e}")
        
    return None
