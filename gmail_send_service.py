# gmail_send_service.py
# ---------------------------------------------------------------------------
# Phase 7 - Gmail Send Service
# Sends email from consultant Gmail with PDF attachment
# ---------------------------------------------------------------------------

import base64
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders
from typing import Optional, List

# BUG FIX: the rich HTML signature (name/title/LinkedIn/contact card +
# company banner — see email_template.py build_signature_html) was being
# BUILT but never actually SENT: build_mime_message only ever attached a
# MIMEText(body, "plain") part, so every real outgoing application email
# was plain text only, regardless of how much work went into the HTML
# version. It was only ever shown in the in-app Email Preview modal. Now
# accepts an optional html_body (+ inline_images, for the company banner
# via Content-ID) and sends a proper multipart/related(alternative(text,
# html), inline images) message when one is provided — falls back to the
# old plain-text-only behavior when it's not, so nothing else breaks.
BANNER_IMAGE_PATH = os.path.join(os.path.dirname(__file__), "static", "email_assets", "company_banner.png")


def build_mime_message(
    sender: str,
    to: str,
    cc: str,
    subject: str,
    body: str,
    attachment_paths: Optional[List[str]] = None,
    attachment_names: Optional[dict] = None,  # maps path -> original display filename
    html_body: Optional[str] = None,
    inline_images: Optional[List[dict]] = None,
) -> str:
    """
    Build MIME email message with optional attachments (one or many) and
    an optional rich HTML body (with inline images, e.g. the company
    banner) alongside the required plain-text fallback.

    FIX: previously took a single attachment_path, so only ever one file
    could ever be sent even when a consultant/recruiter attached several.
    Now loops over every path given and attaches each one that actually
    exists on disk at build time.

    BUG FIX: attachment filenames previously always fell back to
    os.path.basename(attachment_path) — for a Spaces-backed attachment
    downloaded to a tempfile.mkstemp() path (e.g.
    "/tmp/email_queue_attach_8rjcse9l.pdf"), that's a meaningless random
    name, not the consultant's actual resume filename. attachment_names
    (path -> real display name) is now used when present, matching what
    email_queue.py's process_single_email_queue_item already builds.
    """
    paths = [p for p in (attachment_paths or []) if p]
    attachment_names = attachment_names or {}

    # The text+html alternative is its own sub-part regardless of whether
    # there are file attachments or an inline image — a top-level
    # multipart/mixed (for attachments) or multipart/related (for inline
    # images) each need an alternative part inside them, not a bare
    # MIMEText, or the html_body would end up shown as raw markup by
    # clients that don't understand a stray text/html top-level part.
    if html_body:
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "plain"))
        alt.attach(MIMEText(html_body, "html"))
        core = alt
    else:
        core = MIMEText(body, "plain")

    if inline_images:
        related = MIMEMultipart("related")
        related.attach(core)
        for img in inline_images:
            img_path = img.get("path") or BANNER_IMAGE_PATH
            cid = img.get("cid")
            if not cid or not os.path.exists(img_path):
                continue
            with open(img_path, "rb") as f:
                mime_img = MIMEImage(f.read())
            mime_img.add_header("Content-ID", f"<{cid}>")
            mime_img.add_header("Content-Disposition", "inline", filename=os.path.basename(img_path))
            related.attach(mime_img)
        core = related

    if paths:
        msg = MIMEMultipart("mixed")
        msg.attach(core)
    else:
        msg = core

    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject

    for attachment_path in paths:
        if not os.path.exists(attachment_path):
            continue
        with open(attachment_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
            encoders.encode_base64(part)
            filename = attachment_names.get(attachment_path) or os.path.basename(attachment_path)
            part.add_header(
                "Content-Disposition",
                f"attachment; filename={filename}",
            )
            msg.attach(part)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return raw


def send_via_gmail_api(
    access_token: str,
    from_email: str,
    to_email: str,
    cc_email: str,
    subject: str,
    body: str,
    attachment_paths: Optional[List[str]] = None,
    attachment_names: Optional[dict] = None,
    html_body: Optional[str] = None,
    inline_images: Optional[List[dict]] = None,
) -> dict:
    """
    Send email via Gmail API using consultant's OAuth access token.

    Returns:
        {"gmail_message_id": str, "status": "sent"}
    """
    try:
        import httpx

        raw_message = build_mime_message(
            sender=from_email,
            to=to_email,
            cc=cc_email,
            subject=subject,
            body=body,
            attachment_paths=attachment_paths,
            attachment_names=attachment_names,
            html_body=html_body,
            inline_images=inline_images,
        )

        response = httpx.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"raw": raw_message},
            timeout=30.0,
        )

        if response.status_code == 200:
            data = response.json()
            return {
                "gmail_message_id": data.get("id", ""),
                "status": "sent",
            }
        else:
            raise Exception(f"Gmail API error {response.status_code}: {response.text}")

    except ImportError:
        return {
            "gmail_message_id": "mock-message-id-" + from_email,
            "status": "mock_sent",
        }


async def send_application_email_async(
    access_token: str,
    from_email: str,
    to_email: str,
    cc_email: str,
    subject: str,
    body: str,
    attachment_paths: Optional[List[str]] = None,
    attachment_names: Optional[dict] = None,
    html_body: Optional[str] = None,
    inline_images: Optional[List[dict]] = None,
) -> dict:
    """
    Async wrapper for Gmail send. Used by FastAPI endpoints.
    FIX: kept in sync with build_mime_message/send_via_gmail_api's move
    from a single attachment_path to attachment_paths — this wrapper is
    what callers (e.g. the email queue worker) actually call, so it has
    to accept/forward the same param or every call downstream breaks.

    BUG FIX: this wrapper accepted attachment_paths but silently dropped
    attachment_names on the floor — it was never in the signature, so
    even a caller that built a path->original-filename map (like the
    email queue worker downloading a Spaces object to a randomly-named
    /tmp file) had no way to get that name to build_mime_message. The
    MIME builder then fell back to os.path.basename(attachment_path),
    which for a tempfile.mkstemp() path is a meaningless random name
    (e.g. "email_queue_attach_8rjcse9l.pdf") — that's what recipients
    saw in Gmail instead of the consultant's actual resume filename.
    Now forwarded straight through to send_via_gmail_api.

    Also forwards html_body/inline_images so the rich HTML signature +
    company banner built at queue-creation time actually reaches the
    real outgoing message instead of only ever appearing in the in-app
    Email Preview modal.
    """
    import asyncio

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: send_via_gmail_api(
            access_token=access_token,
            from_email=from_email,
            to_email=to_email,
            cc_email=cc_email,
            subject=subject,
            body=body,
            attachment_paths=attachment_paths,
            attachment_names=attachment_names,
            html_body=html_body,
            inline_images=inline_images,
        ),
    )
    return result


def decrypt_token(encrypted_token: str) -> str:
    """
    Decrypt stored OAuth token.
    In production: use Fernet symmetric encryption.
    For now: base64 decode as placeholder.
    """
    if not encrypted_token:
        return ""
    try:
        return base64.b64decode(encrypted_token.encode()).decode()
    except Exception:
        return encrypted_token


def encrypt_token(raw_token: str) -> str:
    """
    Encrypt OAuth token for storage.
    In production: use Fernet symmetric encryption.
    For now: base64 encode as placeholder.
    """
    if not raw_token:
        return ""
    return base64.b64encode(raw_token.encode()).decode()

def get_service_account_access_token(service_account_path: str, impersonate_email: str) -> str:
    """
    Get an OAuth access token using a Service Account with Domain-Wide Delegation.
    """
    import json
    import jwt
    import time
    import httpx
    import os
    
    if not os.path.exists(service_account_path):
        env_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if env_path and os.path.exists(env_path):
            service_account_path = env_path
        else:
            raise Exception("Gmail not connected via OAuth and service-account-key.json is missing.")
            
    with open(service_account_path, "r") as f:
        credentials = json.load(f)
        
    now = int(time.time())
    payload = {
        "iss": credentials["client_email"],
        "sub": impersonate_email,
        "scope": "https://www.googleapis.com/auth/gmail.send",
        "aud": credentials["token_uri"],
        "iat": now,
        "exp": now + 3600
    }
    
    signed_jwt = jwt.encode(
        payload,
        credentials["private_key"],
        algorithm="RS256"
    )
    
    response = httpx.post(
        credentials["token_uri"],
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": signed_jwt
        },
        timeout=10.0
    )
    if response.status_code != 200:
        raise Exception(f"Google OAuth failed with {response.status_code}: {response.text}")
    response.raise_for_status()
    return response.json()["access_token"]