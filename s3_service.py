import os
import boto3
from botocore.exceptions import NoCredentialsError, ClientError

# Configuration from environment variables
DO_SPACES_KEY = os.getenv("DO_SPACES_KEY") or os.getenv("AWS_ACCESS_KEY_ID")
DO_SPACES_SECRET = os.getenv("DO_SPACES_SECRET") or os.getenv("AWS_SECRET_ACCESS_KEY")
DO_SPACES_REGION = os.getenv("DO_SPACES_REGION", "nyc3") # Default DO region
DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET") or os.getenv("AWS_S3_BUCKET")
DO_SPACES_ENDPOINT = os.getenv("DO_SPACES_ENDPOINT", f"https://{DO_SPACES_REGION}.digitaloceanspaces.com")

# Initialize S3 client for DigitalOcean Spaces
s3_client = boto3.client(
    's3',
    endpoint_url=DO_SPACES_ENDPOINT,
    aws_access_key_id=DO_SPACES_KEY,
    aws_secret_access_key=DO_SPACES_SECRET,
    region_name=DO_SPACES_REGION
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
    except (NoCredentialsError, ClientError) as e:
        print(f"Failed to upload to DO Spaces: {e}")
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
    except ClientError as e:
        print(f"Failed to generate presigned URL: {e}")
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
    except ClientError as e:
        print(f"Failed to delete file from DO Spaces: {e}")
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
    except ClientError:
        return None, None