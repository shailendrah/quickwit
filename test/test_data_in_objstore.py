"""
test_data_in_objstore.py — Summarize parquet files in OCI for the demo tables.

Lists s3://$OCI_BUCKET/polaris-iceberg/demo.db/<table>/data/*.parquet for
each of users, orders, products and prints file count + total size. Useful
to confirm the polaris generator wrote what you expected before kicking off
the parquet → ndjson → quickwit pipeline.

Run:
    source ../.venv/bin/activate
    python test/test_data_in_objstore.py
"""

import os
import sys

import boto3
from botocore.config import Config


TABLES = ["users", "orders", "products"]
# Polaris (Iceberg-native namespace naming) writes tables under
# <warehouse>/<namespace>/<table>/, not <warehouse>/<namespace>.db/<table>/.
WAREHOUSE_PREFIX = "polaris-iceberg/demo"


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(f"ERROR: {name} is not set in the environment\n")
        sys.exit(1)
    return val


def _make_s3_client():
    region = _require_env("OCI_REGION")
    return boto3.client(
        "s3",
        endpoint_url=f"https://vhcompat.objectstorage.{region}.oci.customer-oci.com",
        aws_access_key_id=_require_env("OCI_ACCESS_KEY"),
        aws_secret_access_key=_require_env("OCI_SECRET_ACCESS_KEY"),
        region_name=region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def summarize_table(s3, bucket: str, table: str) -> tuple[int, int]:
    """Return (file_count, total_bytes) for parquet files under a table's data/."""
    prefix = f"{WAREHOUSE_PREFIX}/{table}/data/"
    files = 0
    total_bytes = 0
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            if obj["Key"].endswith(".parquet"):
                files += 1
                total_bytes += obj["Size"]
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return files, total_bytes


def main() -> int:
    bucket = _require_env("OCI_BUCKET")
    s3 = _make_s3_client()
    print(f"Bucket:   {bucket}")
    print(f"Prefix:   {WAREHOUSE_PREFIX}/<table>/data/")
    print()
    for tbl in TABLES:
        files, total_bytes = summarize_table(s3, bucket, tbl)
        print(
            f"{tbl:>10}: {files:>4} parquet files, "
            f"{total_bytes / 1024 / 1024:>9.1f} MiB"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
