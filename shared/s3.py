import boto3
import io
import re
import os
import zipfile

_BUCKET = os.environ.get("S3_BUCKET", "xtract-annual-report")
_REGION = os.environ.get("AWS_REGION", "eu-north-1")
_DEFAULT_PRESIGNED_EXPIRY = 604800  # 7 days; AWS S3 presigned URL max for SigV4


def _client():
    return boto3.client(
        "s3",
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        region_name=_REGION,
    )


def upload_pdf(pdf_bytes: bytes, run_id: str, agent: str, filename: str) -> str:
    """Upload PDF to S3. Returns the S3 key."""
    agent_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", agent)[:60]
    key = f"{run_id}/{agent_slug}/{filename}"
    _client().put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=pdf_bytes,
        ContentType="application/pdf",
    )
    return key


def upload_bytes(data: bytes, key: str, content_type: str = "application/octet-stream") -> str:
    """Upload arbitrary bytes to S3. Returns the key."""
    _client().put_object(Bucket=_BUCKET, Key=key, Body=data, ContentType=content_type)
    return key


def get_presigned_url(key: str, expires: int = _DEFAULT_PRESIGNED_EXPIRY) -> str:
    """Return a presigned GET URL valid for `expires` seconds (default 7 days)."""
    if not key:
        return ""
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": _BUCKET, "Key": key},
        ExpiresIn=expires,
    )


def build_run_zip(run_id: str) -> tuple[bytes | None, str]:
    """
    Stream all PDFs for run_id from S3 into a zip, re-upload it to S3, and return
    (zip_bytes, presigned_url).  Returns (None, "") if no PDFs found.

    The bytes are returned so callers (e.g. the email sender) can attach the zip
    directly; the presigned URL is always provided as a fallback for big zips.
    """
    client = _client()
    prefix = f"{run_id}/"

    paginator = client.get_paginator("list_objects_v2")
    pdf_keys = [
        obj["Key"]
        for page in paginator.paginate(Bucket=_BUCKET, Prefix=prefix)
        for obj in page.get("Contents", [])
        if obj["Key"].endswith(".pdf")
    ]

    if not pdf_keys:
        return None, ""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for key in pdf_keys:
            body = client.get_object(Bucket=_BUCKET, Key=key)["Body"].read()
            arcname = key[len(prefix):]
            zf.writestr(arcname, body)

    zip_bytes = buf.getvalue()
    zip_key = f"{run_id}/export/all_reports.zip"
    client.put_object(
        Bucket=_BUCKET,
        Key=zip_key,
        Body=zip_bytes,
        ContentType="application/zip",
    )
    return zip_bytes, get_presigned_url(zip_key)


def generate_run_zip_url(run_id: str) -> str:
    """Backward-compatible helper: build the zip and return only the presigned URL."""
    _, url = build_run_zip(run_id)
    return url
