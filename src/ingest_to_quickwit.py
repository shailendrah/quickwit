"""
Stream the NDJSON.gz objects in OCI Object Storage into Quickwit's ingest
REST endpoint, in 1000-line batches. The last batch of each index uses
?commit=force so docs are searchable immediately rather than after the
indexer's commit_timeout_secs.

Source layout:
    s3://$OCI_BUCKET/quickwit/articles/part-0000.jsonl.gz   →  index `articles`
    s3://$OCI_BUCKET/quickwit/app_logs/part-0000.jsonl.gz   →  index `app_logs`

Run:
    source ../.venv/bin/activate
    python src/ingest_to_quickwit.py
"""

import gzip
import io
import os
import sys
import urllib.error
import urllib.request

import boto3
from botocore.config import Config


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(f"ERROR: {name} is not set in the environment\n")
        sys.exit(1)
    return val


OCI_REGION = _require_env("OCI_REGION")
OCI_BUCKET = _require_env("OCI_BUCKET")
OCI_ACCESS_KEY = _require_env("OCI_ACCESS_KEY")
OCI_SECRET_ACCESS_KEY = _require_env("OCI_SECRET_ACCESS_KEY")

S3_ENDPOINT = f"https://vhcompat.objectstorage.{OCI_REGION}.oci.customer-oci.com"

QUICKWIT_URL = os.environ.get("QUICKWIT_URL", "http://localhost:7280")
BATCH_SIZE = int(os.environ.get("INGEST_BATCH_SIZE", "1000"))

INGEST_TARGETS = [
    ("articles", "quickwit/articles/part-0000.jsonl.gz"),
    ("app_logs", "quickwit/app_logs/part-0000.jsonl.gz"),
]


def _make_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=OCI_ACCESS_KEY,
        aws_secret_access_key=OCI_SECRET_ACCESS_KEY,
        region_name=OCI_REGION,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def _post_batch(index: str, batch: bytes, force_commit: bool) -> None:
    url = f"{QUICKWIT_URL}/api/v1/{index}/ingest"
    if force_commit:
        url += "?commit=force"
    req = urllib.request.Request(
        url,
        data=batch,
        headers={"Content-Type": "application/x-ndjson"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        sys.stderr.write(
            f"ERROR ingesting to {index}: HTTP {e.code} {e.reason}\n  {body}\n"
        )
        sys.exit(1)
    except urllib.error.URLError as e:
        sys.stderr.write(
            f"ERROR connecting to Quickwit at {QUICKWIT_URL}: {e.reason}\n"
            f"  Is the node running? Try: make quickwit-up\n"
        )
        sys.exit(1)


def ingest(s3, index: str, key: str) -> int:
    obj = s3.get_object(Bucket=OCI_BUCKET, Key=key)
    raw = io.BytesIO(obj["Body"].read())
    total = 0
    batch_buf = io.BytesIO()
    batch_lines = 0
    with gzip.GzipFile(fileobj=raw, mode="rb") as gz:
        for line in gz:
            batch_buf.write(line)
            batch_lines += 1
            total += 1
            if batch_lines >= BATCH_SIZE:
                _post_batch(index, batch_buf.getvalue(), force_commit=False)
                batch_buf = io.BytesIO()
                batch_lines = 0
    if batch_lines:
        _post_batch(index, batch_buf.getvalue(), force_commit=True)
    elif total > 0:
        # Final batch landed exactly on a boundary — issue a tiny no-op force
        # commit by POSTing an empty body so the last full batch publishes.
        _post_batch(index, b"", force_commit=True)
    return total


def main() -> None:
    s3 = _make_s3_client()
    print(f"Quickwit:  {QUICKWIT_URL}")
    print(f"OCI:       s3://{OCI_BUCKET}/  (endpoint={S3_ENDPOINT})")
    print(f"Batch:     {BATCH_SIZE} lines per POST")
    print()
    for index, key in INGEST_TARGETS:
        print(f"--- {index}  <-  s3://{OCI_BUCKET}/{key} ---")
        n = ingest(s3, index, key)
        print(f"  ingested {n} docs")
    print("\nDone. Last batch per index used ?commit=force.")


if __name__ == "__main__":
    main()
