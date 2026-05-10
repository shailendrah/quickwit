"""
Generate fake document-oriented data with Faker and write it as
gzipped NDJSON to OCI Object Storage (S3-compatible API), in a layout
Quickwit can ingest via its file/S3 source.

Two indexes:
  - articles  — id, title, author, published_at, body, tags[], category,
                source, language, url, word_count
  - app_logs  — timestamp, level, service, host, client_ip, user_id,
                trace_id, span_id, message, attributes{}, latency_ms,
                status_code, http_method, http_path

Layout in the bucket (reuses the polaris-iceberg bucket; separate prefix):

    s3://polaris-iceberg/quickwit/articles/part-0000.jsonl.gz
    s3://polaris-iceberg/quickwit/app_logs/part-0000.jsonl.gz

Each line is one full document — i.e., {colname1: value1, colname2: value2, ...}
— which is exactly what Quickwit's NDJSON ingestion expects.

Prereqs (one-time, in the central venv at frameworks/.venv):
    source ../.venv/bin/activate
    pip install faker boto3

Run:
    source ../.venv/bin/activate
    # All of these come from ~/.zshrc:
    #   OCI_NAMESPACE, OCI_REGION, OCI_BUCKET,
    #   OCI_ACCESS_KEY, OCI_SECRET_ACCESS_KEY
    python src/generate_quickwit_data.py
"""

import gzip
import io
import json
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config
from faker import Faker


# ---- Config ---------------------------------------------------------------


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(f"ERROR: {name} is not set in the environment\n")
        sys.exit(1)
    return val


OCI_NAMESPACE = _require_env("OCI_NAMESPACE")
OCI_REGION = _require_env("OCI_REGION")
OCI_BUCKET = _require_env("OCI_BUCKET")
OCI_ACCESS_KEY = _require_env("OCI_ACCESS_KEY")
OCI_SECRET_ACCESS_KEY = _require_env("OCI_SECRET_ACCESS_KEY")

# OCI's vhcompat endpoint carries a wildcard cert covering bucket subdomains,
# so default virtual-hosted-style addressing works without TLS errors.
S3_ENDPOINT = f"https://vhcompat.objectstorage.{OCI_REGION}.oci.customer-oci.com"

# Top-level prefix for everything Quickwit-related in the shared bucket.
QW_PREFIX = os.environ.get("QW_PREFIX", "quickwit")

ARTICLES_ROWS = int(os.environ.get("ARTICLES_ROWS", "2000"))
LOGS_ROWS = int(os.environ.get("LOGS_ROWS", "20000"))


# ---- Faker setup ----------------------------------------------------------


faker = Faker()
faker.seed_instance(42)
random.seed(42)


# ---- articles -------------------------------------------------------------


CATEGORIES = [
    "technology",
    "business",
    "science",
    "health",
    "sports",
    "politics",
    "entertainment",
    "world",
]
SOURCES = ["The Daily Wire", "Globe Times", "Tech Pulse", "Science Now", "Market Watch", "Sports Beat"]
LANGUAGES = ["en", "en", "en", "en", "es", "fr", "de"]  # weighted toward English


def gen_article(article_id: int) -> dict:
    published_at = faker.date_time_between(
        start_date="-2y", end_date="now", tzinfo=timezone.utc
    )
    paragraphs = faker.paragraphs(nb=random.randint(3, 8))
    body = "\n\n".join(paragraphs)
    tags = list({faker.word() for _ in range(random.randint(2, 6))})
    return {
        "id": article_id,
        "title": faker.sentence(nb_words=random.randint(6, 14)).rstrip("."),
        "author": faker.name(),
        "published_at": published_at.isoformat(),
        "body": body,
        "tags": tags,
        "category": random.choice(CATEGORIES),
        "source": random.choice(SOURCES),
        "language": random.choice(LANGUAGES),
        "url": faker.url() + faker.uri_path(),
        "word_count": len(body.split()),
    }


def gen_articles(n: int):
    for i in range(n):
        yield gen_article(i + 1)


# ---- app_logs -------------------------------------------------------------


SERVICES = [
    "checkout-api",
    "auth-service",
    "search-frontend",
    "recommendation-engine",
    "billing-worker",
    "ingest-pipeline",
    "user-profile",
    "notification-service",
]
HTTP_METHODS = ["GET", "GET", "GET", "POST", "POST", "PUT", "DELETE"]
HTTP_PATHS = [
    "/api/v1/users",
    "/api/v1/users/{id}",
    "/api/v1/orders",
    "/api/v1/orders/{id}",
    "/api/v1/search",
    "/api/v1/checkout",
    "/api/v1/products",
    "/healthz",
    "/metrics",
]
LEVELS_WEIGHTED = (
    ["INFO"] * 70 + ["DEBUG"] * 15 + ["WARN"] * 10 + ["ERROR"] * 4 + ["FATAL"]
)
ERROR_TEMPLATES = [
    "connection refused to upstream {host}:{port}",
    "request timed out after {ms}ms calling {svc}",
    "failed to deserialize payload field {field}",
    "circuit breaker tripped for {svc}",
    "rate limit exceeded for tenant {tenant}",
    "database query failed: {err}",
    "unable to acquire lock on resource {res}",
    "invalid auth token for user {user}",
]
INFO_TEMPLATES = [
    "request handled in {ms}ms",
    "cache hit for key {key}",
    "scheduled job {job} completed",
    "user {user} signed in",
    "published event {event} to topic {topic}",
    "warmed up connection pool size={size}",
]


def _render_log_message(level: str) -> str:
    if level in ("ERROR", "FATAL", "WARN"):
        tpl = random.choice(ERROR_TEMPLATES)
        return tpl.format(
            host=faker.domain_name(),
            port=random.choice([5432, 6379, 9092, 8080, 443]),
            ms=random.randint(800, 30000),
            svc=random.choice(SERVICES),
            field=random.choice(["user_id", "order_id", "amount", "tags", "metadata"]),
            tenant=faker.uuid4(),
            err=faker.sentence(nb_words=6).rstrip("."),
            res=f"order:{random.randint(1, 99999)}",
            user=faker.user_name(),
        )
    tpl = random.choice(INFO_TEMPLATES)
    return tpl.format(
        ms=random.randint(1, 500),
        key=faker.slug(),
        job=random.choice(["nightly-rollup", "stale-session-sweep", "metric-flush"]),
        user=faker.user_name(),
        event=random.choice(["order.placed", "user.signed_up", "payment.captured"]),
        topic=random.choice(["events.orders", "events.users", "events.audit"]),
        size=random.choice([4, 8, 16, 32, 64]),
    )


def gen_log(start_window: datetime) -> dict:
    # Spread events over a 14-day window leading up to start_window.
    ts = start_window - timedelta(seconds=random.randint(0, 14 * 24 * 3600))
    level = random.choice(LEVELS_WEIGHTED)
    service = random.choice(SERVICES)
    method = random.choice(HTTP_METHODS)
    path = random.choice(HTTP_PATHS).replace(
        "{id}", str(random.randint(1, 100000))
    )
    status = (
        random.choice([200, 200, 200, 201, 204, 301, 302])
        if level in ("INFO", "DEBUG")
        else random.choice([400, 401, 403, 404, 409, 429, 500, 502, 503, 504])
    )
    return {
        "timestamp": ts.isoformat().replace("+00:00", "Z"),
        "level": level,
        "service": service,
        "host": f"{service}-{random.randint(0, 9)}.prod.internal",
        "client_ip": faker.ipv4_public(),
        "user_id": random.randint(1, 50000) if random.random() < 0.7 else None,
        "trace_id": uuid.UUID(int=random.getrandbits(128)).hex,
        "span_id": uuid.UUID(int=random.getrandbits(64)).hex[:16],
        "message": _render_log_message(level),
        "http_method": method,
        "http_path": path,
        "status_code": status,
        "latency_ms": round(random.expovariate(1 / 50.0), 2),
        "attributes": {
            "region": random.choice(["us-east-1", "us-west-2", "eu-west-1", "ap-south-1"]),
            "deployment": random.choice(["v1.42.3", "v1.42.4", "v1.43.0"]),
            "tenant": random.choice(["acme", "globex", "initech", "umbrella"]),
        },
    }


def gen_logs(n: int):
    now = datetime.now(timezone.utc)
    for _ in range(n):
        yield gen_log(now)


# ---- NDJSON.gz writer to OCI ----------------------------------------------


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
            # OCI's S3-compat doesn't accept aws-chunked for PutObject; only
            # send checksums when truly required.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def write_ndjson_gz(s3, key: str, rows) -> tuple[int, int]:
    """Serialize rows to gzipped NDJSON and PUT to s3://OCI_BUCKET/key.

    Returns (row_count, compressed_bytes).
    """
    raw = io.BytesIO()
    n = 0
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        for row in rows:
            line = json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n"
            gz.write(line.encode("utf-8"))
            n += 1
    body = raw.getvalue()
    s3.put_object(
        Bucket=OCI_BUCKET,
        Key=key,
        Body=body,
        ContentType="application/x-ndjson",
        ContentEncoding="gzip",
    )
    return n, len(body)


# ---- Main -----------------------------------------------------------------


def main() -> None:
    s3 = _make_s3_client()

    print(f"Endpoint: {S3_ENDPOINT}")
    print(f"Bucket:   {OCI_BUCKET}")
    print(f"Prefix:   {QW_PREFIX}/")
    print()

    articles_key = f"{QW_PREFIX}/articles/part-0000.jsonl.gz"
    logs_key = f"{QW_PREFIX}/app_logs/part-0000.jsonl.gz"

    print(f"--- articles (target rows={ARTICLES_ROWS}) ---")
    n, sz = write_ndjson_gz(s3, articles_key, gen_articles(ARTICLES_ROWS))
    print(f"  wrote s3://{OCI_BUCKET}/{articles_key}  rows={n}  size={sz / 1024:.1f} KiB")

    print(f"\n--- app_logs (target rows={LOGS_ROWS}) ---")
    n, sz = write_ndjson_gz(s3, logs_key, gen_logs(LOGS_ROWS))
    print(f"  wrote s3://{OCI_BUCKET}/{logs_key}  rows={n}  size={sz / 1024:.1f} KiB")

    print("\nDone.")
    print(
        f"Browse OCI Object Storage:  oci os object list --namespace {OCI_NAMESPACE} "
        f"--bucket-name {OCI_BUCKET} --prefix {QW_PREFIX}/"
    )


if __name__ == "__main__":
    main()
