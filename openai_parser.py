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

PARSE_REQUIREMENT_SYSTEM_PROMPT = """You are a job requirement parsing engine. You will be given the raw subject and body of an email, sent by a recruiter or staffing vendor, describing a job opening.
Extract its content using the extract_requirement JSON schema.
If a field is not present or cannot be confidently determined, leave it as null (or an empty list for list fields) — do not guess, and never copy text meant for one field into a different one.

ROLE
- Extract the specific job title being staffed (e.g. "Senior Java Developer", "Scrum Master"). This is very often stated with an explicit label ("Role:", "Title:", "Job Title:", "Position:") — use that when it's there. It is just as often stated only in plain prose with NO label at all (e.g. "We are looking for a Senior React Developer to join the team", "Seeking a DevOps Engineer for a 6-month contract") — extract the title from context in that case with the same confidence as a labeled one. Don't leave role null just because there's no colon-separated label; most real postings don't have one.
- The one thing to avoid: when there is NO role label AND no clear prose statement of the title either, do not fall back to substituting a DIFFERENT field's labeled value (Visa, Interview Mode, Work Authorization, Duration, Rate, Client, Client Name, etc.) as if it were the role — leave role null in that specific situation instead.
- Never return a bare employment type or work-arrangement word (e.g. "Remote", "Contract", "C2C") as the role.
- Never return a generic template/section header (e.g. "Job Title", "Role", "Position", "Requirement") as the role — that is a placeholder, not a real title.

CLIENT
- "client" means the END company the consultant will actually work for/at — it is NEVER the recruiter, staffing agency, or vendor who is sending this email, even though that company's name is often visible in the signature, "Thanks & Regards" block, or letterhead.
- The client can be named through a label ("Client:", "End Client:") OR in plain prose ("This position is with Northern Trust", "on-site at Deloitte's downtown office") — both count as a real, explicit client name and should be extracted the same way. What does NOT count: a vague, unnamed reference ("one of our clients," "a Fortune 500 company," "our client") or no mention at all — leave client null in those cases. Do not infer or guess a client from context.
- Never return the role's own technology, product, or platform name as the client (e.g. a role of "Oracle EBS Upgrade" does not make the client "Oracle" or "EBS" unless a company by that literal name is separately, explicitly named as the client).
- Never copy text from the subject line into the client field.
- Never return a generic template label itself (e.g. "Client Name", "End Client") as if it were a real company name.
- "client-facing" is an extremely common phrase describing a SKILL, not a labeled field (e.g. "excellent client-facing communication skills," "strong client-facing experience"). Never treat the word "client" inside this phrase as a label and extract whatever follows "facing" as if it were a company name — that is never a real client.
- A section heading that describes what the end client wants in a candidate — e.g. "What the Client is Looking For," "Client Requirements," "Client Expectations" — is NOT a statement naming who the client is. It introduces a description of desired skills/experience, not a company name. Extract a real client only if one is separately, explicitly named elsewhere; do not extract any part of the heading itself (e.g. never "Looking For").
- A bare country or region name (USA, India, Canada, etc.) is NEVER a client — even when it sits right next to a dash near other job details (e.g. "Remote – USA," or a signature listing office locations like "USA - INDIA"). These are locations or the vendor's own office locations, never a company name.
- If nothing in the email actually names a client, do not manufacture one by pulling in a nearby unrelated sentence, a responsibility bullet, or any other stray text just because the client field expects a value — leave it null. A wrong guess is far worse than an honest null.

EMPLOYMENT TYPE
- Watch for negation: "No C2C", "not open to C2C", "no H1B" means that value should NOT be included as an accepted type.
- Only extract an employment type that is stated as part of the actual job posting itself (e.g. "Employment Type: Contract," "C2C/W2/1099," a role description). Never infer one from unsubscribe text, mailing-list or distribution-group names, sender signatures, or any other footer/boilerplate content below the actual posting — a mailing list named something like "C2C Requirements" or "Contract Jobs Daily" says nothing about whether THIS specific posting is C2C or contract.

SKILLS
- List only concrete skills/technologies actually requested — never a table's own column heading (e.g. "Skill", "Description") from a skills table.
- Extract EVERY concrete skill, technology, tool, or platform explicitly named — don't be selective when several are listed together. If the email lists five testing types ("functional, integration, UI/UX, regression, and UAT testing"), include all five, not just some of them.
- When a SPECIFIC named product or platform is mentioned (e.g. "Salesforce Marketing Cloud", "Braze", "Prisma Cloud"), extract that specific name — don't reduce it down to only a vague general-category paraphrase (e.g. don't drop "Salesforce Marketing Cloud" and keep only "marketing automation"; include the actual product name that was written, alongside a general category term if the email also uses one).

When in doubt on any field, prefer leaving it null over guessing — a wrong value is worse than a missing one.
"""

PARSE_REQUIREMENT_SCHEMA = {
    "name": "extract_requirement",
    "description": "Record the structured fields extracted from a job requirement email.",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "role": {
                "type": ["string", "null"],
                "description": "The specific job title being staffed — from an explicit 'Role:'/'Title:'/'Job Title:' label OR from plain prose with no label at all (e.g. 'seeking a Senior React Developer'); both are equally valid. Never the value of an unrelated field (Visa, Interview Mode, Work Authorization, Duration, Rate, Client) substituted in when no role is stated, a bare employment-type/work-mode word, or a generic header like 'Job Title'."
            },
            "client": {
                "type": ["string", "null"],
                "description": "The END client company where the consultant will work — from a label ('Client:') OR named in plain prose ('this role is with Northern Trust'). NEVER the recruiter/vendor/staffing agency sending this email, even if that company's name appears in the signature. NEVER a country/region name (USA, India). NEVER extracted from 'client-facing' (a skill phrase, not a label) or a heading like 'What the Client is Looking For'. Leave null for a vague reference ('one of our clients'), no mention at all, or when nothing genuinely names a client — do not manufacture a value from unrelated nearby text."
            },
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
                # BUG FIX (schema drift vs. Claude's copy of this schema, and
                # vs. the canonical EMPLOYMENT_KEYWORDS vocabulary used by the
                # regex fallback elsewhere in this codebase): "C2H"
                # (contract-to-hire) is a real, common employment type this
                # backend already recognizes everywhere else -- it was
                # missing here only, so OpenAI could never return it even
                # when an email explicitly said "Contract to Hire".
                "items": {"type": "string", "enum": ["C2C", "C2H", "W2", "1099", "FULLTIME", "CONTRACT", "UNKNOWN"]},
                "description": "One or more of C2C, C2H, W2, 1099, FULLTIME, CONTRACT, or UNKNOWN. Do not include a type the email explicitly negates (e.g. 'No C2C'). Only from the actual posting content — never inferred from a mailing-list/group name or unsubscribe footer text."
            },
            "experience": {"type": ["string", "null"], "description": "e.g. '8+ years'."},
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key skills and technologies requested — never a table column heading like 'Skill' or 'Description'. Extract ALL explicitly named skills/tools/platforms, not a selective subset. Prefer a specific named product (e.g. 'Salesforce Marketing Cloud') over only a vague paraphrase of it."
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