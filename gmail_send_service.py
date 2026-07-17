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
from email import encoders
from typing import Optional


def build_mime_message(
    sender: str,
    to: str,
    cc: str,
    subject: str,
    body: str,
    attachment_path: Optional[str] = None,
) -> str:
    """
    Build MIME email message with optional PDF attachment.
    Returns base64url encoded string for Gmail API.
    """
    if attachment_path:
        msg = MIMEMultipart()
    else:
        msg = MIMEMultipart("alternative")

    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject

    msg.attach(MIMEText(body, "plain"))

    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
            encoders.encode_base64(part)
            filename = os.path.basename(attachment_path)
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
    attachment_path: Optional[str] = None,
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
            attachment_path=attachment_path,
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
    attachment_path: Optional[str] = None,
) -> dict:
    """Async wrapper for Gmail send. Used by FastAPI endpoints."""
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
            attachment_path=attachment_path,
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
    response.raise_for_status()
    return response.json()["access_token"]