# Quickwit demo data generator

Generates fake document-oriented data with [Faker](https://faker.readthedocs.io/)
and writes it as gzipped NDJSON to OCI Object Storage (S3-compatible API),
in a layout Quickwit can ingest via its file/S3 source.

Two indexes:

| Index | What it shows off |
|-------|-------------------|
| `articles` | Full-text search over titles + multi-paragraph bodies, faceting by category/tag/language, time-range queries by `published_at`. |
| `app_logs` | The "Quickwit was built for this" use case — timestamp + level + service filtering, free-text search on `message`, JSON `attributes` field, IP type, latency aggregates. |

Both write to the same OCI bucket you already use for Polaris (`$OCI_BUCKET`,
typically `polaris-iceberg`), under a separate `quickwit/` prefix:

    s3://polaris-iceberg/quickwit/articles/part-0000.jsonl.gz
    s3://polaris-iceberg/quickwit/app_logs/part-0000.jsonl.gz

## Setup

The central venv at `frameworks/.venv` already has most of what's needed. If
`faker` isn't installed:

    source ../.venv/bin/activate
    pip install faker boto3

Required env vars (already in `~/.zshrc` for the Polaris setup):

    OCI_REGION
    OCI_NAMESPACE
    OCI_BUCKET            # reuses polaris-iceberg
    OCI_ACCESS_KEY
    OCI_SECRET_ACCESS_KEY

Optional:

    QW_PREFIX=quickwit              # bucket prefix for these files
    ARTICLES_ROWS=2000
    LOGS_ROWS=20000

## Generate the data

    source ../.venv/bin/activate
    python src/generate_quickwit_data.py

Confirm it landed:

    oci os object list --namespace $OCI_NAMESPACE \
        --bucket-name $OCI_BUCKET --prefix quickwit/

## Wire it up to Quickwit

Once Quickwit is running locally (and its metastore is pointed at the Postgres
DB we set up — `postgres://skmishra:skmishra@localhost:5432/quickwit_metastore`):

    # 1) Tell Quickwit the OCI S3-compat endpoint.
    export QW_S3_ENDPOINT="https://vhcompat.objectstorage.${OCI_REGION}.oci.customer-oci.com"
    export AWS_ACCESS_KEY_ID="$OCI_ACCESS_KEY"
    export AWS_SECRET_ACCESS_KEY="$OCI_SECRET_ACCESS_KEY"
    export AWS_REGION="$OCI_REGION"

    # 2) Create the two indexes from the configs in this directory.
    quickwit index create --index-config src/index-articles.yaml
    quickwit index create --index-config src/index-app-logs.yaml

    # 3) Ingest. Easiest path: pull the NDJSON.gz down and pipe to ingest.
    aws --endpoint-url "$QW_S3_ENDPOINT" s3 cp \
        "s3://$OCI_BUCKET/quickwit/articles/part-0000.jsonl.gz" - \
        | gunzip | quickwit index ingest --index articles

    aws --endpoint-url "$QW_S3_ENDPOINT" s3 cp \
        "s3://$OCI_BUCKET/quickwit/app_logs/part-0000.jsonl.gz" - \
        | gunzip | quickwit index ingest --index app_logs

## Sample queries

    # Articles
    curl 'http://localhost:7280/api/v1/articles/search?query=technology+AND+category:business'
    curl 'http://localhost:7280/api/v1/articles/search?query=*&start_timestamp=...&end_timestamp=...'

    # Logs
    curl 'http://localhost:7280/api/v1/app_logs/search?query=level:ERROR+AND+service:checkout-api'
    curl 'http://localhost:7280/api/v1/app_logs/search?query=status_code:>=500'
    curl 'http://localhost:7280/api/v1/app_logs/search?query=client_ip:10.0.0.0/8'
