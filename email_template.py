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
"""


# ---------------------------------------------------------------------------
# Signature block — sender's own contact card, appended after "Thanks &
# Regards,". Previously the sign-off was just three plain text lines
# (consultant_name/phone/email) baked into DEFAULT_TEMPLATE, and always
# showed the CONSULTANT's contact info even when an admin/recruiter sent
# the application on their behalf — the vendor had no way to know who to
# actually reply to. Now built from whoever is actually SENDING the email
# (current_user in phase7.py), with the consultant's own info used only
# when they're applying for themselves. HTML version matches the signature
# card design provided (name, title, LinkedIn, Email/Direct/Text/Address),
# plus the real company banner image supplied — embedded inline via CID
# (see COMPANY_BANNER_CID / BANNER_IMAGE_PATH), not as a regular
# attachment, so it renders inline in the signature like the sample.
# ---------------------------------------------------------------------------

COMPANY_NAME = "Savantis Intelli Solutions"
COMPANY_TAGLINE = "Quality is not an act, it is a habit."
COMPANY_ADDRESS = "Dallas, Texas, USA"
COMPANY_LINE_NUMBER = "+1 469-392-4030"
# Referenced as <img src="cid:{COMPANY_BANNER_CID}"> in build_signature_html
# below, and attached inline (Content-ID header, not a regular attachment)
# by gmail_send_service.build_mime_message — see BANNER_IMAGE_PATH there.
COMPANY_BANNER_CID = "company_banner_img"

# BUG FIX: previously a static file at static/email_assets/company_banner.png
# — updating the banner meant manually copying a new file onto every
# server (local + production separately), and a mismatch between them
# was invisible until someone actually sent/previewed an email. Now
# stored in Spaces (same bucket resumes/attachments already use) under
# this one fixed key, always overwritten in place on update (see the
# admin upload endpoint in main.py) — so replacing it once is
# immediately live everywhere, no file copying or redeploy needed.
# gmail_send_service.py fetches this fresh at send time (falling back to
# the old static file only if Spaces isn't configured/reachable), and
# the preview banner_src below now points at a backend endpoint that
# proxies the same S3 object, instead of the old static file.
COMPANY_BANNER_S3_KEY = "company-assets/banner.png"


def build_signature_text(
    name: str,
    title: Optional[str],
    email: Optional[str],
    direct_number: Optional[str],
    extension: Optional[str],
    linkedin_url: Optional[str],
) -> str:
    """Plain-text fallback signature — used as the multipart/plain part of
    the sent email, and shown in the (plain-text) preview UI."""
    lines = [name]
    if title:
        lines.append(title)
    if linkedin_url:
        lines.append(linkedin_url)
    lines.append("")
    if email:
        lines.append(f"E: {email}")
    if direct_number:
        lines.append(f"D: {direct_number}")
    if extension:
        lines.append(f"T: {COMPANY_LINE_NUMBER} EXT {extension}")
    lines.append(f"A: {COMPANY_ADDRESS}")
    lines.append("")
    lines.append(COMPANY_NAME)
    lines.append(f'"{COMPANY_TAGLINE}"')
    return "\n".join(lines)


def build_signature_html(
    name: str,
    title: Optional[str],
    email: Optional[str],
    direct_number: Optional[str],
    extension: Optional[str],
    linkedin_url: Optional[str],
    *,
    banner_src: str = f"cid:{COMPANY_BANNER_CID}",
) -> str:
    """Rich HTML signature — matches the provided card layout. Sent as the
    multipart/html part (see gmail_send_service.build_mime_message), so it
    only actually renders in HTML-capable email clients; plain-text
    clients fall back to build_signature_text() above.

    banner_src defaults to the cid: reference used for the actual sent
    email (see BANNER_IMAGE_PATH/COMPANY_BANNER_S3_KEY in
    gmail_send_service.py, which attaches the real file inline under that
    Content-ID). A browser can't resolve cid: URLs on its own, so the
    Email Preview modal instead passes a real servable URL
    (/api/settings/company-banner — see get_email_preview in phase7.py)
    so the banner actually shows up there too, not just in the real sent
    email.
    """
    import html as _html

    def esc(s: Optional[str]) -> str:
        return _html.escape(s) if s else ""

    contact_rows = []
    if email:
        contact_rows.append(f'<b>E:</b> <a href="mailto:{esc(email)}" style="color:#2563eb;text-decoration:underline;">{esc(email)}</a>')
    if direct_number:
        contact_rows.append(f'<b>D:</b> {esc(direct_number)}')
    if extension:
        contact_rows.append(f'<b>T:</b> {esc(COMPANY_LINE_NUMBER)} EXT {esc(extension)}')
    contact_rows.append(f'<b>A:</b> {esc(COMPANY_ADDRESS)}')
    contact_html = "<br>".join(contact_rows)

    linkedin_html = (
        f'<div style="margin-top:8px;"><a href="{esc(linkedin_url)}" style="color:#2563eb;text-decoration:underline;font-weight:600;">{esc(linkedin_url)}</a></div>'
        if linkedin_url else ""
    )
    title_html = f'<div style="font-style:italic;color:#334155;margin-top:2px;">{esc(title)}</div>' if title else ""

    return f"""
<table cellpadding="0" cellspacing="0" style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#334155;margin-top:12px;">
  <tr>
    <td style="vertical-align:top;padding-right:20px;">
      <div style="font-weight:700;color:#0f766e;font-size:14px;">{esc(name)}</div>
      {title_html}
      {linkedin_html}
    </td>
    <td style="vertical-align:top;border-left:1px solid #cbd5e1;padding-left:20px;line-height:1.6;">
      {contact_html}
    </td>
  </tr>
</table>
<div style="margin-top:14px;">
  <img src="{esc(banner_src)}" alt="{esc(COMPANY_NAME)} — &quot;{esc(COMPANY_TAGLINE)}&quot;" style="max-width:600px;width:100%;display:block;border:0;">
</div>
""".strip()


def render_email_template(
    template: Optional[str] = None,
    vendor_contact_name: Optional[str] = None,
    role: str = "",
    top_skills: str = "",
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


# ---------------------------------------------------------------------------
# Shared signature/sender resolution — phase7.py (older preview/confirm-send
# endpoints) and this module both need it, so it lives here in
# email_template.py rather than either of theirs, avoiding a circular
# import between the two (phase7.py already imports from email_queue.py,
# and email_queue.py needs this too — see send_email_now there, the ACTUAL
# live endpoint the Apply buttons on Pending Applications/Requirements hit;
# phase7.py's confirm_send/get_email_preview turned out not to be wired to
# those buttons at all, just an earlier/alternate implementation).
# ---------------------------------------------------------------------------
ROLE_TITLE = {"ADMIN": "Administrator", "RECRUITER": "Recruiter"}


# ---------------------------------------------------------------------------
# Signature images — see the upload/serve endpoints in main.py. A saved
# custom signature holds real <img src=".../api/settings/signature-image/
# <key>"> URLs (works fine for in-browser previews), but the actual
# outbound email must never reference that URL directly — many clients
# block remote images by default, and a locally-running dev backend isn't
# even reachable by an external recipient's mail client at all. This
# rewrites each one to a cid: reference and returns the {cid, key} pairs
# so the caller (process_single_email_queue_item in email_queue.py, which
# already downloads/attaches the company banner the same way) can
# download the actual bytes from Spaces and attach them inline.
# ---------------------------------------------------------------------------
import re as _re_sigimg

SIGNATURE_IMAGE_URL_RE = _re_sigimg.compile(
    r'src="[^"]*?/api/settings/signature-image/(signature-images/[^"]+?)"'
)


def rewrite_signature_images_for_send(html: Optional[str]):
    """Returns (rewritten_html, [{"cid": str, "key": str}, ...])."""
    if not html:
        return html, []

    images = []
    seen = {}

    def _replace(match):
        key = match.group(1)
        if key not in seen:
            seen[key] = f"sig_img_{len(seen)}"
            images.append({"cid": seen[key], "key": key})
        return f'src="cid:{seen[key]}"'

    rewritten = SIGNATURE_IMAGE_URL_RE.sub(_replace, html)
    return rewritten, images


def resolve_sender_fields(current_user, consultant) -> dict:
    """Build the signature's sender identity from whoever is actually
    applying (current_user) — an admin/recruiter's own contact info when
    they're sending on a consultant's behalf, or the consultant's own
    profile info when they're applying for themselves. Title prefers the
    user's own designation (real job title) when set, else a generic
    role-based label.

    Always builds the default signature card from these fields — the
    custom signature editor/save feature was removed."""
    if current_user.role in ("ADMIN", "RECRUITER"):
        return {
            "sender_name": current_user.full_name or "",
            "sender_title": getattr(current_user, "designation", None) or ROLE_TITLE.get(current_user.role),
            "sender_email": current_user.email or "",
            "sender_direct_number": getattr(current_user, "mobile_number", None),
            "sender_extension": getattr(current_user, "extension", None),
            "sender_linkedin_url": getattr(current_user, "linkedin_url", None),
        }

    return {
        "sender_name": (consultant.full_name if consultant else "") or "",
        "sender_title": "Consultant",
        "sender_email": (consultant.email if consultant else "") or "",
        "sender_direct_number": consultant.phone if consultant else None,
        "sender_extension": None,
        "sender_linkedin_url": getattr(consultant, "linkedin_url", None) if consultant else None,
    }


def build_application_email(
    vendor_contact_name: Optional[str],
    role: str,
    consultant_name: str,
    consultant_email: str,
    consultant_phone: Optional[str],
    primary_skills: Optional[str],
    custom_template: Optional[str] = None,
    *,
    sender_name: str = "",
    sender_title: Optional[str] = None,
    sender_email: Optional[str] = None,
    sender_direct_number: Optional[str] = None,
    sender_extension: Optional[str] = None,
    sender_linkedin_url: Optional[str] = None,
    sender_signature: Optional[str] = None,
    for_preview: bool = False,
) -> dict:
    """
    Build complete email content for an application.

    The intro paragraph is still about the consultant (their resume, their
    background) — only the sign-off signature identifies the SENDER
    (whoever is actually applying: an admin/recruiter on the consultant's
    behalf, or the consultant themselves), since that's who the vendor
    should actually reply to.

    for_preview=True (used by get_email_preview in phase7.py) points the
    banner image at a real servable URL instead of a cid: reference, since
    a browser rendering the Email Preview modal can't resolve cid: — only
    an actual email client can, once gmail_send_service attaches the image
    inline under that Content-ID at send time.

    Returns:
        {"subject": str, "body": str, "html_body": str, "preview": str,
         "inline_images": [{"cid": str}]}
    """
    top_skills = extract_top_skills(primary_skills)
    subject = build_email_subject(role, consultant_name)
    intro = render_email_template(
        template=custom_template,
        vendor_contact_name=vendor_contact_name,
        role=role,
        top_skills=top_skills,
    )

    # Fall back to the consultant's own info if no sender identity was
    # given at all (keeps this function usable exactly as before for any
    # other caller that hasn't been updated to pass sender_* yet).
    sig_name = sender_name or consultant_name
    sig_email = sender_email or consultant_email
    sig_direct = sender_direct_number or consultant_phone

    if sender_signature:
        html_signature = f'<div style="margin-top:12px;">{sender_signature}</div>'
        # Simple tag strip to generate the plain text version of the custom signature
        import re
        clean_text_sig = re.sub(r'<[^>]*>', '', sender_signature)
        text_signature = f"\n\n{clean_text_sig.strip()}"
    else:
        text_signature = build_signature_text(
            sig_name, sender_title, sig_email, sig_direct, sender_extension, sender_linkedin_url,
        )
        banner_kwargs = {"banner_src": "/api/settings/company-banner"} if for_preview else {}
        html_signature = build_signature_html(
            sig_name, sender_title, sig_email, sig_direct, sender_extension, sender_linkedin_url,
            **banner_kwargs,
        )

    body = intro + text_signature
    html_body = (
        "<div style=\"font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1e293b;white-space:pre-wrap;\">"
        + intro.rstrip("\n").replace("\n", "<br>")
        + "</div>"
        + html_signature
    )

    return {
        "subject": subject,
        "body": body,
        "html_body": html_body,
        "preview": body[:500],
        # gmail_send_service.build_mime_message attaches this inline
        # (Content-ID, not a regular attachment) when html_body is sent —
        # see BANNER_IMAGE_PATH there for where the actual file lives.
        "inline_images": [{"cid": COMPANY_BANNER_CID}],
    }