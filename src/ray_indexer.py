"""
ray_indexer.py — Ray-based parallel ingest into Quickwit.

Reads .zip files (each containing one or more NDJSON members) from a folder
(OCI prefix s3://... or a local directory) and routes the rows through a
fixed pool of Indexer actors that POST to Quickwit's /api/v1/{index}/ingest.

Topology:
    driver
      ├── stateless reader tasks    @ray.remote def read_batch(uris) -> rows
      └── pool of N Indexer actors  @ray.remote class Indexer
            actors[batch_no % N].index.remote(rows_ref)

`actor.index.remote(rows_ref)` resolves the ObjectRef on the actor's worker,
so the rows produced by the reader task hand off via Ray's object store —
no driver-side blocking, no extra copy through the driver.

Backpressure: the driver caps in-flight futures at num_indexers * mult and
drains via ray.wait when at the cap.

Idempotency caveat: Quickwit's /ingest does not deduplicate by default; if
a batch fails mid-way and is retried, you'll get duplicate documents unless
the index doc mapping declares a doc-id field. (Out of scope for this demo
script — fix at the index level if needed.)

Run:
    source ../.venv/bin/activate
    python src/ray_indexer.py \\
        --input s3://$OCI_BUCKET/enrichedsi/ \\
        --index <index_id> \\
        --quickwit http://localhost:7280
"""

import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from typing import Iterator

import boto3
import ray
from botocore.config import Config


# ---- OCI / S3 client (same shape as ingest_to_quickwit.py) ----------------


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


# ---- Listing input zip files ----------------------------------------------


def list_zip_uris(input_uri: str) -> list[str]:
    """List every .zip under input_uri (s3://bucket/prefix/ or local dir)."""
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
                if obj["Key"].endswith(".zip"):
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
        os.path.join(base, name) for name in os.listdir(base) if name.endswith(".zip")
    )


# ---- Reader: stateless task -----------------------------------------------


def _read_bytes(uri: str) -> bytes:
    if uri.startswith("s3://"):
        bucket, key = _parse_s3_uri(uri)
        return _make_s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
    with open(uri, "rb") as f:
        return f.read()


def _iter_ndjson(zip_bytes: bytes) -> Iterator[dict]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            with zf.open(name) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    yield json.loads(line)


@ray.remote
def read_batch(uris: list[str]) -> list[dict]:
    """Download `uris` (zip files), decode every NDJSON member into a flat list."""
    rows: list[dict] = []
    for uri in uris:
        for row in _iter_ndjson(_read_bytes(uri)):
            rows.append(row)
    return rows


# ---- Indexer: stateful actor ----------------------------------------------


@ray.remote
class Indexer:
    """POSTs rows to Quickwit's /ingest endpoint in chunks of `chunk_size`."""

    def __init__(self, quickwit_url: str, index_id: str, chunk_size: int):
        self.url = f"{quickwit_url.rstrip('/')}/api/v1/{index_id}/ingest"
        self.chunk_size = chunk_size

    def _post(self, body: bytes, force: bool = False) -> None:
        url = self.url + ("?commit=force" if force else "")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-ndjson"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"ingest POST {url} -> HTTP {e.code} {e.reason}: {err_body}"
            ) from e

    def index(self, rows: list[dict]) -> dict:
        sent = 0
        body_bytes = 0
        t0 = time.monotonic()
        for i in range(0, len(rows), self.chunk_size):
            chunk = rows[i : i + self.chunk_size]
            body = (
                "\n".join(
                    json.dumps(r, separators=(",", ":"), ensure_ascii=False)
                    for r in chunk
                )
                + "\n"
            ).encode("utf-8")
            self._post(body)
            sent += len(chunk)
            body_bytes += len(body)
        return {
            "rows": sent,
            "bytes": body_bytes,
            "wall_s": time.monotonic() - t0,
        }

    def force_commit(self) -> None:
        # Empty body + ?commit=force flushes any pending docs in the indexer.
        self._post(b"", force=True)


# ---- Driver ---------------------------------------------------------------


def _chunks(seq: list, n: int) -> Iterator[list]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _drain_one(in_flight: list, meta: dict) -> dict:
    done, remaining = ray.wait(in_flight, num_returns=1)
    in_flight[:] = remaining
    ref = done[0]
    return {"meta": meta.pop(ref, {}), "result": ray.get(ref)}


def _print_done(d: dict) -> None:
    m, r = d["meta"], d["result"]
    print(
        f"batch {m.get('batch_no', '?'):>4}: "
        f"{r['rows']:>7d} rows, "
        f"{r['bytes'] / 1024 / 1024:>6.1f} MiB, "
        f"{r['wall_s']:5.1f}s "
        f"(actor {m.get('actor_idx', '?'):>2})"
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--input",
        required=True,
        help="s3://bucket/prefix/ or local dir of .zip files",
    )
    p.add_argument("--index", required=True, help="Quickwit index id")
    p.add_argument(
        "--quickwit",
        default=os.environ.get("QUICKWIT_URL", "http://localhost:7280"),
    )
    p.add_argument("--num-indexers", type=int, default=16)
    p.add_argument(
        "--batch-size",
        type=int,
        default=16,
        help="zip files per reader task",
    )
    p.add_argument(
        "--ingest-chunk",
        type=int,
        default=1000,
        help="rows per /ingest POST",
    )
    p.add_argument(
        "--in-flight-mult",
        type=int,
        default=2,
        help="cap in-flight futures at num_indexers * this",
    )
    p.add_argument(
        "--ray-address",
        default=None,
        help="Ray cluster address (omit for a local Ray instance)",
    )
    args = p.parse_args()

    uris = list_zip_uris(args.input)
    if not uris:
        sys.stderr.write(f"ERROR: no .zip files under {args.input}\n")
        return 1

    print(f"Quickwit:    {args.quickwit}")
    print(f"Index:       {args.index}")
    print(f"Input:       {args.input}")
    print(f"Found:       {len(uris)} zip files")
    print(f"Indexers:    {args.num_indexers}")
    print(f"Batch:       {args.batch_size} zip files / reader task")
    print(f"Chunk:       {args.ingest_chunk} rows / POST")
    print()

    ray.init(address=args.ray_address)
    try:
        actors = [
            Indexer.remote(args.quickwit, args.index, args.ingest_chunk)
            for _ in range(args.num_indexers)
        ]

        in_flight: list = []
        meta: dict = {}
        cap = args.num_indexers * args.in_flight_mult
        total_rows = 0
        total_bytes = 0
        t_start = time.monotonic()

        for batch_no, batch in enumerate(_chunks(uris, args.batch_size)):
            while len(in_flight) >= cap:
                d = _drain_one(in_flight, meta)
                total_rows += d["result"]["rows"]
                total_bytes += d["result"]["bytes"]
                _print_done(d)

            rows_ref = read_batch.remote(batch)
            actor_idx = batch_no % args.num_indexers
            idx_ref = actors[actor_idx].index.remote(rows_ref)
            in_flight.append(idx_ref)
            meta[idx_ref] = {"batch_no": batch_no, "actor_idx": actor_idx}

        while in_flight:
            d = _drain_one(in_flight, meta)
            total_rows += d["result"]["rows"]
            total_bytes += d["result"]["bytes"]
            _print_done(d)

        # One force-commit so docs are searchable immediately, matching the
        # behavior of ingest_to_quickwit.py's final batch.
        ray.get(actors[0].force_commit.remote())

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
