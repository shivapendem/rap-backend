import os
import json
import logging
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# We use a cheap and fast model for parsing
OPENAI_MODEL = os.getenv("OPENAI_PARSER_MODEL", "gpt-4o-mini")

PARSE_REQUIREMENT_SYSTEM_PROMPT = """You are a job requirement parsing engine. You will be given the raw subject and body of an email containing a job requirement.
Extract its content using the extract_requirement JSON schema.
If a field is not present or cannot be confidently determined, leave it as null (or an empty list for list fields) — do not guess.
"""

PARSE_REQUIREMENT_SCHEMA = {
    "name": "extract_requirement",
    "description": "Record the structured fields extracted from a job requirement email.",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "role": {"type": ["string", "null"], "description": "The job title."},
            "client": {"type": ["string", "null"], "description": "The end client or company, if explicitly mentioned."},
            "location": {"type": ["string", "null"], "description": "City, state, or Remote/Hybrid/Onsite."},
            "rate": {"type": ["string", "null"], "description": "The pay/bill rate or compensation."},
            "duration": {"type": ["string", "null"], "description": "e.g. '6 months', 'long term'."},
            "work_mode": {
                "type": ["string", "null"],
                "enum": ["REMOTE", "HYBRID", "ONSITE", "UNKNOWN"],
                "description": "REMOTE, HYBRID, ONSITE, or UNKNOWN."
            },
            "employment_types": {
                "type": "array",
                "items": {"type": "string", "enum": ["C2C", "W2", "1099", "FULLTIME", "CONTRACT", "UNKNOWN"]},
                "description": "One or more of C2C, W2, 1099, FULLTIME, CONTRACT, or UNKNOWN."
            },
            "experience": {"type": ["string", "null"], "description": "e.g. '8+ years'."},
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key skills and technologies requested."
            },
        },
        "required": ["role", "client", "location", "rate", "duration", "work_mode", "employment_types", "experience", "skills"],
        "additionalProperties": False
    },
}

def parse_requirement_openai(subject: str, body: str) -> Optional[dict]:
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY not found, skipping OpenAI parser.")
        return None

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        
        user_prompt = f"SUBJECT: {subject}\n\nBODY: {body}"
        
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": PARSE_REQUIREMENT_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}
            ],
            tools=[{"type": "function", "function": PARSE_REQUIREMENT_SCHEMA}],
            tool_choice={"type": "function", "function": {"name": "extract_requirement"}}
        )
        
        message = response.choices[0].message
        if message.tool_calls:
            arguments = message.tool_calls[0].function.arguments
            parsed = json.loads(arguments)
            parsed["parsing_model"] = f"OpenAI ({OPENAI_MODEL})"
            
            # Convert nulls in work_mode/employment_types if any sneaked in
            if not parsed.get("work_mode"):
                parsed["work_mode"] = "UNKNOWN"
            if not parsed.get("employment_types"):
                parsed["employment_types"] = ["UNKNOWN"]
                
            return parsed
            
    except Exception as e:
        logger.warning(f"Error calling OpenAI API for parsing: {e}")
        
    return None
