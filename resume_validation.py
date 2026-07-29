# resume_validation.py
# ---------------------------------------------------------------------------
# Single source of truth for "is this profile complete enough to generate a
# resume from". Both generation entry points hit this:
#   - resume_router.py  POST /api/resume/generate            ("My Resumes")
#   - phase6.py          POST /api/consultant/requirements/{id}/generate-resume
#
# Manager ask: we were never checking that the profile actually had real
# data in it before calling the AI, so a thin profile silently produced a
# useless resume. This checks every section that shows up in a real
# resume_info blob (header fields, summary, skills, experience, education),
# not just a subset -- and returns *which* fields are missing so the caller
# (API error, or the frontend pre-check) can tell the user exactly what to
# go fill in.
# ---------------------------------------------------------------------------

from typing import Any, Dict, List, Optional


def _str(value: Any) -> str:
    """Coerce to a stripped string; None/non-str-y values become ''."""
    if value is None:
        return ""
    return str(value).strip()


def _non_empty_str(resume_info: Dict[str, Any], *keys: str) -> bool:
    return any(_str(resume_info.get(k)) for k in keys)


def _non_empty_skills(resume_info: Dict[str, Any]) -> bool:
    if resume_info.get("skills"):
        return True
    tech_stack = resume_info.get("tech_stack") or {}
    combined = (
        (tech_stack.get("expert") or [])
        + (tech_stack.get("familiar") or [])
        + (tech_stack.get("exposure") or [])
        + (tech_stack.get("intermediate") or [])
    )
    if combined:
        return True
    # Real imported/parsed profiles (e.g. via the resume JSON import feature)
    # commonly store skills only as a categorized table under
    # technical_proficiencies: [{"category": ..., "skills": [...]}, ...] --
    # no flat "skills" key and no "tech_stack" at all. Same source
    # claude_service.py's normalization and mock fallback already treat as
    # a legitimate skills source; this check needs to agree, or real,
    # fully-populated profiles get incorrectly blocked.
    tech_profs = resume_info.get("technical_proficiencies") or []
    for row in tech_profs:
        if isinstance(row, dict) and row.get("skills"):
            return True
    return False


def _non_empty_experience(resume_info: Dict[str, Any]) -> bool:
    return bool(resume_info.get("experience"))


def _non_empty_education(resume_info: Dict[str, Any]) -> bool:
    return bool(resume_info.get("education") or resume_info.get("educational_background"))


def _non_empty_summary(resume_info: Dict[str, Any]) -> bool:
    return _non_empty_str(
        resume_info, "summary", "career_objective", "professional_summary", "objective"
    )


def _non_empty_title(resume_info: Dict[str, Any]) -> bool:
    return _non_empty_str(resume_info, "title", "target_role", "target_title")


def _non_empty_years_experience(resume_info: Dict[str, Any]) -> bool:
    for key in ("years_experience", "total_experience_years"):
        val = resume_info.get(key)
        if val not in (None, ""):
            return True
    return False


# Every field we require, in the order they should be reported.
# (field_key, human label, checker function)
FIELD_CHECKS = [
    ("full_name", "Full Name", lambda ri: _non_empty_str(ri, "full_name")),
    ("email", "Email", lambda ri: _non_empty_str(ri, "email")),
    ("phone", "Phone", lambda ri: _non_empty_str(ri, "phone")),
    ("title", "Target Title / Role", _non_empty_title),
    ("summary", "Summary", _non_empty_summary),
    ("linkedin", "LinkedIn", lambda ri: _non_empty_str(ri, "linkedin")),
    ("years_experience", "Years of Experience", _non_empty_years_experience),
    ("skills", "Skills", _non_empty_skills),
    ("experience", "Experience", _non_empty_experience),
    ("education", "Education", _non_empty_education),
]

FIELD_LABELS = {key: label for key, label, _ in FIELD_CHECKS}


def get_missing_resume_fields(resume_info: Optional[Dict[str, Any]]) -> List[str]:
    """
    Returns the list of field keys that are missing/empty in resume_info,
    checked against every field that shows up in a real profile (header
    info, summary, skills, experience, education -- see FIELD_CHECKS above).
    Empty list = profile is complete enough to generate a real resume.
    """
    resume_info = resume_info or {}
    missing: List[str] = []
    for key, _label, check_fn in FIELD_CHECKS:
        if not check_fn(resume_info):
            missing.append(key)
    return missing


def missing_fields_message(missing: List[str]) -> str:
    labels = [FIELD_LABELS.get(f, f) for f in missing]
    if len(labels) == 1:
        joined = labels[0]
    elif len(labels) == 2:
        joined = f"{labels[0]} and {labels[1]}"
    else:
        joined = ", ".join(labels[:-1]) + f", and {labels[-1]}"
    return (
        f"Add {joined} to the profile before generating a resume. "
        "Generating without them produces an unusable resume."
    )