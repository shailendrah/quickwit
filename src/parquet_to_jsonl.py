"""
parquet_to_jsonl.py — Ray-based Parquet → NDJSON.zip transcoder.

Reads .parquet files for a table (a folder/prefix containing one or more
.parquet files) and emits .zip files of NDJSON in the layout that
ray_indexer.py expects.

Topology (Ray Core, two stateless tasks chained via ObjectRef):

    driver enumerates parquet files
        │
        ├── rows_ref = read_parquet_file.remote(uri, columns)      # task A
        ├── out_uri   = derive_out_uri(output, uri)
        ├── write_ref = write_zip_jsonl.remote(rows_ref, out_uri)  # task B
        │     (Ray resolves rows_ref on B's worker — no driver-side blocking,
        │      no extra copy through the driver)
        └── ray.wait(write_refs)  # pthread_join

Each task is sync `def` — parallelism comes from process-level distribution,
not coroutines. Both decorated with num_cpus=0.25 because the work is
I/O-bound (S3 read + JSON encode + S3 write); oversubscribing 4x lets many
tasks run concurrently per CPU.

One Parquet file = one input split. PyArrow row groups are not split out
into separate tasks here; the driver doesn't need to read any per-file
metadata. For huge multi-row-group files where finer-grained parallelism
matters, pre-split upstream or refactor to enumerate (file, row_group)
pairs (cheap if you wire pyarrow.fs.S3FileSystem so the driver reads only
the Parquet footer).

Run:
    source ../.venv/bin/activate
    python src/parquet_to_jsonl.py \\
        --input  s3://$OCI_BUCKET/parquet/X/ \\
        --output s3://$OCI_BUCKET/enrichedsi/ \\
        --columns col1,col2,col3
"""

import argparse
import datetime as _dt
import io
import json
import os
import sys
import time
import zipfile
from typing import Any, Iterator

import boto3
import pyarrow.parquet as pq
import ray
from botocore.config import Config


def _json_default(o: Any) -> str:
    """Serialize Python values json.dumps doesn't natively know about.

    Parquet timestamps come back through PyArrow's to_pylist() as tz-aware
    `datetime` objects. `str(dt)` produces "YYYY-MM-DD HH:MM:SS+00:00" (space
    separator), which fails Quickwit's `input_formats: [rfc3339]`. We emit
    canonical RFC 3339 with `T` and a `Z` suffix for UTC, matching the format
    `generate_quickwit_data.py` writes.
    """
    if isinstance(o, _dt.datetime):
        s = o.isoformat()
        if s.endswith("+00:00"):
            s = s[:-6] + "Z"
        return s
    if isinstance(o, _dt.date):
        return o.isoformat()
    return str(o)


# ---- OCI / S3 client (matches ingest_to_quickwit.py / ray_indexer.py) -----


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


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    rest = uri[len("s3://") :]
    bucket, _, key = rest.partition("/")
    return bucket, key


def _read_bytes(uri: str) -> bytes:
    if uri.startswith("s3://"):
        bucket, key = _parse_s3_uri(uri)
        return _make_s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
    with open(uri, "rb") as f:
        return f.read()


def _write_bytes(uri: str, data: bytes) -> None:
    if uri.startswith("s3://"):
        bucket, key = _parse_s3_uri(uri)
        _make_s3_client().put_object(Bucket=bucket, Key=key, Body=data)
        return
    parent = os.path.dirname(uri)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(uri, "wb") as f:
        f.write(data)


# ---- Listing input parquet files ------------------------------------------


def list_parquet_uris(input_uri: str) -> list[str]:
    """List every .parquet under input_uri (s3://bucket/prefix/ or local dir)."""
    if input_uri.startswith("s3://"):
        bucket, prefix = _parse_s3_uri(input_uri)
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        s3 = _make_s3_client()
        keys: list[str] = []
        token = None
        while True:
            kw = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = s3.list_objects_v2(**kw)
            for obj in resp.get("Contents", []):
                if obj["Key"].endswith(".parquet"):
                    keys.append(f"s3://{bucket}/{obj['Key']}")
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return sorted(keys)

    base = input_uri[len("file://") :] if input_uri.startswith("file://") else input_uri
    if not os.path.isdir(base):
        sys.stderr.write(f"ERROR: not a directory: {base}\n")
        sys.exit(1)
    return sorted(
        os.path.join(base, name)
        for name in os.listdir(base)
        if name.endswith(".parquet")
    )


# ---- Reader task: parquet bytes -> list[dict] -----------------------------


@ray.remote(num_cpus=0.25)
def read_parquet_file(parquet_uri: str, columns: list[str] | None) -> list[dict]:
    """Read one .parquet file, return rows as list[dict].

    Whole file is materialized in worker memory; for huge multi-row-group
    files, switch to a row-group-aware enumeration in the driver.
    """
    raw = _read_bytes(parquet_uri)
    table = pq.read_table(io.BytesIO(raw), columns=columns)
    return table.to_pylist()


# ---- Writer task: list[dict] -> .zip (NDJSON inside) ----------------------


def _zip_member_name(out_uri: str) -> str:
    return os.path.splitext(os.path.basename(out_uri))[0] + ".ndjson"


@ray.remote(num_cpus=0.25)
def write_zip_jsonl(rows: list[dict], out_uri: str) -> dict:
    """Serialize rows to NDJSON inside a .zip at out_uri (S3 or local)."""
    if not rows:
        return {"rows": 0, "bytes": 0, "out_uri": out_uri}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        with zf.open(_zip_member_name(out_uri), "w") as f:
            for r in rows:
                line = json.dumps(
                    r,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    default=_json_default,
                )
                f.write(line.encode("utf-8"))
                f.write(b"\n")
    data = buf.getvalue()
    _write_bytes(out_uri, data)
    return {"rows": len(rows), "bytes": len(data), "out_uri": out_uri}


# ---- Driver ---------------------------------------------------------------


def _derive_out_uri(output_prefix: str, parquet_uri: str) -> str:
    stem = os.path.splitext(os.path.basename(parquet_uri))[0]
    name = f"sid_ndjson_{stem}.zip"
    if output_prefix.endswith("/"):
        return output_prefix + name
    return f"{output_prefix}/{name}"


def _drain_one(in_flight: list, meta: dict) -> dict:
    done, remaining = ray.wait(in_flight, num_returns=1)
    in_flight[:] = remaining
    ref = done[0]
    return {"meta": meta.pop(ref, {}), "result": ray.get(ref)}


def _print_done(d: dict) -> None:
    m, r = d["meta"], d["result"]
    print(
        f"split {m.get('split_no', '?'):>4}: "
        f"{r['rows']:>7d} rows, "
        f"{r['bytes'] / 1024 / 1024:>6.1f} MiB → "
        f"{os.path.basename(r['out_uri'])}"
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--input",
        required=True,
        help="s3://bucket/prefix/ or local dir of .parquet files (table X)",
    )
    p.add_argument(
        "--output",
        required=True,
        help="s3://bucket/prefix/ or local dir for .zip outputs",
    )
    p.add_argument(
        "--columns",
        default=None,
        help="comma-separated columns to project (default: all)",
    )
    p.add_argument(
        "--in-flight",
        type=int,
        default=64,
        help="cap on outstanding write futures (driver-side backpressure)",
    )
    p.add_argument(
        "--ray-address",
        default=None,
        help="Ray cluster address (omit for a local Ray instance)",
    )
    args = p.parse_args()

    columns = (
        [c.strip() for c in args.columns.split(",") if c.strip()]
        if args.columns
        else None
    )

    uris = list_parquet_uris(args.input)
    if not uris:
        sys.stderr.write(f"ERROR: no .parquet files under {args.input}\n")
        return 1

    print(f"Input:    {args.input}")
    print(f"Output:   {args.output}")
    print(f"Columns:  {columns or '(all)'}")
    print(f"Found:    {len(uris)} parquet files")
    print(f"Cap:      {args.in_flight} in-flight writes")
    print()

    ray.init(address=args.ray_address)
    try:
        in_flight: list = []
        meta: dict = {}
        total_rows = 0
        total_bytes = 0
        t_start = time.monotonic()

        for split_no, uri in enumerate(uris):
            while len(in_flight) >= args.in_flight:
                d = _drain_one(in_flight, meta)
                total_rows += d["result"]["rows"]
                total_bytes += d["result"]["bytes"]
                _print_done(d)

            rows_ref = read_parquet_file.remote(uri, columns)
            out_uri = _derive_out_uri(args.output, uri)
            wref = write_zip_jsonl.remote(rows_ref, out_uri)
            in_flight.append(wref)
            meta[wref] = {"split_no": split_no, "uri": uri, "out_uri": out_uri}

        while in_flight:
            d = _drain_one(in_flight, meta)
            total_rows += d["result"]["rows"]
            total_bytes += d["result"]["bytes"]
            _print_done(d)

        elapsed = time.monotonic() - t_start
        print()
        print(
            f"Done: {total_rows} rows, "
            f"{total_bytes / 1024 / 1024:.1f} MiB in {elapsed:.1f}s "
            f"({total_rows / elapsed:.0f} rows/s, "
            f"{total_bytes / 1024 / 1024 / elapsed:.1f} MiB/s)"
        )
        return 0
    finally:
        ray.shutdown()


if __name__ == "__main__":
    sys.exit(main())
