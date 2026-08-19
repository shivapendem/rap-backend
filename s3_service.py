import os
import boto3
from pathlib import Path
from botocore.config import Config
from botocore.exceptions import NoCredentialsError, ClientError, BotoCoreError
from dotenv import load_dotenv

# BUG FIX ("DO Spaces bucket not configured" even though DO_SPACES_BUCKET
# is set in .env): this file never called load_dotenv() itself — it read
# every DO_SPACES_* env var directly at import time and just hoped some
# OTHER module (database.py, claude_service.py, openai_service.py — the
# only three files in this codebase that actually call load_dotenv())
# had already run first and populated os.environ from .env. Module-level
# code only executes once, on a module's FIRST import — if anything ever
# imports s3_service before one of those three modules runs (a different
# entrypoint, a changed import order, a future refactor), DO_SPACES_BUCKET
# etc. read back as None permanently for that process's whole lifetime,
# with no error at startup — it only surfaces later as this exact
# "not configured" message the first time an upload is attempted. Calling
# load_dotenv() here too (same one-line pattern already used by database.py
# et al.) makes this file self-sufficient and immune to import order
# entirely, matching how every other config-reading module in this
# codebase is written.
#
# FOLLOW-UP FIX (still "not configured" after deploying the above,
# confirmed against a verified-correct .env): plain load_dotenv() with no
# path argument searches for a .env file starting from the process's
# CURRENT WORKING DIRECTORY, not from wherever this .py file itself lives
# on disk. If the real server starts the backend from a different working
# directory than this folder — extremely common with systemd services
# that don't set WorkingDirectory=, or with some process managers/Docker
# CMDs — that search finds nothing, silently, and every DO_SPACES_* var
# below still reads back as None even though the .env file is sitting
# right here and is completely correct. Pointing load_dotenv() at this
# file's own directory explicitly (via __file__) makes it immune to the
# server's working directory entirely — it always finds THIS folder's
# .env, no matter where the process was launched from.
#
# FOLLOW-UP FIX #2 (still "not configured" even with the __file__-based
# path above): python-dotenv's load_dotenv() defaults to override=False
# — it will NOT overwrite a variable that already exists in the process's
# real OS environment, even if that existing value is an empty string.
# Some deployment setups (systemd Environment= lines, a Docker env file,
# a hosting platform's env panel) define DO_SPACES_BUCKET as a blank
# placeholder meant to be filled in later, or a stray leftover from an
# earlier config — if that's the case here, the .env file's correct
# value would be silently ignored forever, no matter how correct the
# .env is or how reliably it's found. override=True makes .env
# authoritative for these specific DO_SPACES_* variables — there's no
# legitimate case where an accidental blank OS-level value should win
# over a real one intentionally set in .env.
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=True)

# Configuration from environment variables
DO_SPACES_KEY = os.getenv("DO_SPACES_KEY") or os.getenv("AWS_ACCESS_KEY_ID")
DO_SPACES_SECRET = os.getenv("DO_SPACES_SECRET") or os.getenv("AWS_SECRET_ACCESS_KEY")
DO_SPACES_REGION = os.getenv("DO_SPACES_REGION", "nyc3") # Default DO region
DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET") or os.getenv("AWS_S3_BUCKET")
DO_SPACES_ENDPOINT = os.getenv("DO_SPACES_ENDPOINT", f"https://{DO_SPACES_REGION}.digitaloceanspaces.com")

# BUG FIX (network/firewall failures hang for boto3's default ~60s
# connect/read timeout, then raise an exception type the functions below
# didn't catch at all — see the broadened except clauses further down):
# an explicit, shorter timeout means a genuinely unreachable Spaces
# endpoint (wrong endpoint, DNS failure, firewall silently dropping
# packets) fails fast instead of leaving a request hanging for a minute
# before erroring — and still counts as the kind of failure the except
# clauses below are now built to catch cleanly.
_S3_CLIENT_CONFIG = Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 2})

# Initialize S3 client for DigitalOcean Spaces
s3_client = boto3.client(
    's3',
    endpoint_url=DO_SPACES_ENDPOINT,
    aws_access_key_id=DO_SPACES_KEY,
    aws_secret_access_key=DO_SPACES_SECRET,
    region_name=DO_SPACES_REGION,
    config=_S3_CLIENT_CONFIG,
)

def upload_file_to_s3(file_obj, s3_key: str, content_type: str = "application/pdf", _error_out: list = None) -> bool:
    """Uploads a file object to DigitalOcean Spaces.

    _error_out: optional list — when provided, the real failure reason
    (bucket-not-configured message, or the boto3 exception) is appended
    to it. Lets callers that want to surface *why* an upload failed (e.g.
    in an API error response, so it's visible without server log access)
    opt in, without changing the bool return value every existing caller
    already relies on.
    """
    if not DO_SPACES_BUCKET:
        print("DO Spaces bucket not configured")
        if _error_out is not None:
            _error_out.append("DO Spaces bucket not configured")
        return False

    try:
        s3_client.upload_fileobj(
            file_obj,
            DO_SPACES_BUCKET,
            s3_key,
            ExtraArgs={'ContentType': content_type, 'ACL': 'private'}
        )
        return True
    # BUG FIX (a network/firewall failure reaching Spaces crashed the
    # caller instead of failing gracefully): this only caught
    # NoCredentialsError and ClientError — real, but narrow. A blocked
    # or unreachable endpoint (wrong DO_SPACES_ENDPOINT, DNS failure, a
    # firewall silently dropping packets — exactly the failure mode a
    # "credentials/bucket are correct but Spaces is unreachable from
    # this server" diagnosis would produce) raises
    # EndpointConnectionError/ConnectTimeoutError instead, which are
    # BotoCoreError subclasses, NOT ClientError — they weren't caught at
    # all, so they propagated as an unhandled exception all the way up
    # to the calling endpoint (an unhandled 500 with a raw stack trace,
    # instead of the clean "Failed to upload..." + _error_out reason
    # every other failure mode here already gets). Catching BotoCoreError
    # too — and a final bare Exception as a last-resort safety net —
    # means every possible failure from this call now degrades to the
    # same clean, diagnosable False + _error_out response.
    except (NoCredentialsError, ClientError, BotoCoreError) as e:
        print(f"Failed to upload to DO Spaces: {e}")
        if _error_out is not None:
            _error_out.append(str(e))
        return False
    except Exception as e:
        print(f"Unexpected error uploading to DO Spaces: {e}")
        if _error_out is not None:
            _error_out.append(str(e))
        return False

def generate_presigned_url(s3_key: str, expires_in: int = 3600) -> str:
    """Generates a presigned URL for downloading a file."""
    if not DO_SPACES_BUCKET:
        print("DO Spaces bucket not configured")
        return ""

    try:
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={
                'Bucket': DO_SPACES_BUCKET,
                'Key': s3_key
            },
            ExpiresIn=expires_in
        )
        return url
    # Same broadened-exception fix as upload_file_to_s3 above — presigning
    # is a local operation (no network call), so this mainly guards
    # against a malformed client config rather than connectivity, but the
    # same class of gap applied here too.
    except (ClientError, BotoCoreError) as e:
        print(f"Failed to generate presigned URL: {e}")
        return ""
    except Exception as e:
        print(f"Unexpected error generating presigned URL: {e}")
        return ""

def delete_file_from_s3(s3_key: str) -> bool:
    """Deletes a file from DigitalOcean Spaces."""
    if not DO_SPACES_BUCKET:
        print("DO Spaces bucket not configured")
        return False

    try:
        s3_client.delete_object(
            Bucket=DO_SPACES_BUCKET,
            Key=s3_key
        )
        return True
    except (ClientError, BotoCoreError) as e:
        print(f"Failed to delete file from DO Spaces: {e}")
        return False
    except Exception as e:
        print(f"Unexpected error deleting from DO Spaces: {e}")
        return False

def download_file_from_s3(s3_key: str):
    """
    Fetch an object's bytes from Spaces.
    Returns (body_bytes, content_type) or (None, None) on any failure.
    Used to proxy downloads through the API so browsers never make a
    cross-origin XHR to Spaces (which would require a bucket CORS policy).
    """
    if not DO_SPACES_BUCKET:
        print("DO Spaces bucket not configured")
        return None, None
    try:
        obj = s3_client.get_object(Bucket=DO_SPACES_BUCKET, Key=s3_key)
        return obj["Body"].read(), obj.get("ContentType", "application/pdf")
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        if error_code == "NoSuchKey":
            print(f"[s3_service] File not found in DO Spaces (NoSuchKey): {s3_key}")
        else:
            print(f"Failed to download file from DO Spaces: {e}")
        return None, None
    except BotoCoreError as e:
        print(f"Failed to download file from DO Spaces (connection error): {e}")
        return None, None
    except Exception as e:
        print(f"Unexpected error downloading from DO Spaces: {e}")
        return None, None

def get_s3_file_metadata(s3_key: str):
    """
    Returns (size_bytes, content_type) for an object in Spaces via
    head_object, or (None, None) if it can't be found/read. Used to show
    real attachment sizes in the Email Preview modal without downloading
    the whole file.
    """
    if not DO_SPACES_BUCKET:
        return None, None
    try:
        head = s3_client.head_object(Bucket=DO_SPACES_BUCKET, Key=s3_key)
        return head.get("ContentLength"), head.get("ContentType", "application/octet-stream")
    except (ClientError, BotoCoreError):
        return None, None
    except Exception:
        return None, None