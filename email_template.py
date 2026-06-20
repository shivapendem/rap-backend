# email_template.py
# ---------------------------------------------------------------------------
# Phase 7 - Email Template Service
# Builds configurable email body for application emails
# ---------------------------------------------------------------------------

from typing import Optional


DEFAULT_TEMPLATE = """Hi {vendor_contact_name},

I hope you are doing well.

Please find attached my updated resume for the {role} position.

My background aligns well with this requirement, especially in {top_skills}.

Please let me know if you need any additional details.

Thanks & Regards,
{consultant_name}
{consultant_phone}
{consultant_email}
"""


def render_email_template(
    template: Optional[str] = None,
    vendor_contact_name: Optional[str] = None,
    role: str = "",
    top_skills: str = "",
    consultant_name: str = "",
    consultant_phone: Optional[str] = "",
    consultant_email: str = "",
) -> str:
    """
    Render email template with safe variable replacement.
    Missing vendor name falls back to 'Team'.
    """
    t = template or DEFAULT_TEMPLATE

    safe_values = {
        "vendor_contact_name": vendor_contact_name or "Team",
        "role": role or "the position",
        "top_skills": top_skills or "relevant technologies",
        "consultant_name": consultant_name or "",
        "consultant_phone": consultant_phone or "",
        "consultant_email": consultant_email or "",
    }

    try:
        return t.format(**safe_values)
    except KeyError:
        for key, val in safe_values.items():
            t = t.replace(f"{{{key}}}", val)
        return t


def build_email_subject(role: str, consultant_name: str) -> str:
    """Build standard email subject line."""
    return f"Application for {role} - {consultant_name}"


def extract_top_skills(primary_skills: Optional[str], max_skills: int = 3) -> str:
    """
    Extract top N skills from comma-separated primary_skills string.
    Returns natural language string like 'React, TypeScript, and Node.js'
    """
    if not primary_skills:
        return "relevant technologies"

    skills = [s.strip() for s in primary_skills.split(",") if s.strip()]
    top = skills[:max_skills]

    if not top:
        return "relevant technologies"
    if len(top) == 1:
        return top[0]
    if len(top) == 2:
        return f"{top[0]} and {top[1]}"
    return f"{', '.join(top[:-1])}, and {top[-1]}"


def build_application_email(
    vendor_contact_name: Optional[str],
    role: str,
    consultant_name: str,
    consultant_email: str,
    consultant_phone: Optional[str],
    primary_skills: Optional[str],
    custom_template: Optional[str] = None,
) -> dict:
    """
    Build complete email content for an application.

    Returns:
        {"subject": str, "body": str, "preview": str}
    """
    top_skills = extract_top_skills(primary_skills)
    subject = build_email_subject(role, consultant_name)
    body = render_email_template(
        template=custom_template,
        vendor_contact_name=vendor_contact_name,
        role=role,
        top_skills=top_skills,
        consultant_name=consultant_name,
        consultant_phone=consultant_phone or "",
        consultant_email=consultant_email,
    )

    return {
        "subject": subject,
        "body": body,
        "preview": body[:500],
    }