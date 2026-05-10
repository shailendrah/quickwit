DOCKER_SERVICES ?= all

QUICKWIT_SRC = quickwit

help:
	@grep '^[^\.#[:space:]].*:' Makefile


IMAGE_TAG := $(shell git branch --show-current | tr '\#/' '-')

QW_COMMIT_DATE := $(shell TZ=UTC0 git log -1 --format=%cd --date=format-local:'%Y-%m-%dT%H:%M:%SZ')
QW_COMMIT_HASH := $(shell git rev-parse HEAD)
QW_COMMIT_TAGS := $(shell git tag --points-at HEAD | tr '\n' ',')

docker-build:
	@docker build \
		--build-arg QW_COMMIT_DATE=$(QW_COMMIT_DATE) \
		--build-arg QW_COMMIT_HASH=$(QW_COMMIT_HASH) \
		--build-arg QW_COMMIT_TAGS=$(QW_COMMIT_TAGS) \
		-t quickwit/quickwit:$(IMAGE_TAG) .

# Usage:
# `make docker-compose-up` starts all the services.
# `make docker-compose-up DOCKER_SERVICES='jaeger,localstack'` starts the subset of services matching the profiles.
docker-compose-up:
	@echo "Launching ${DOCKER_SERVICES} Docker service(s)"
	COMPOSE_PROFILES=$(DOCKER_SERVICES) docker compose -f docker-compose.yml up -d --remove-orphans --wait

docker-compose-down:
	docker compose -p quickwit down --remove-orphans

docker-compose-logs:
	docker compose logs -f docker-compose.yml -t

docker-compose-monitoring:
	COMPOSE_PROFILES=monitoring docker compose -f docker-compose.yml up -d --remove-orphans

# Usage:
# `make quickwit-up` starts only the dockerized Quickwit node (profile=quickwit).
# Assumes the host Postgres (separate pgcompose stack) is already running and
# that OCI_ACCESS_KEY / OCI_SECRET_ACCESS_KEY / OCI_REGION are exported in the
# shell. Splits go to OCI; metastore is the host Postgres at
# host.docker.internal:5432/quickwit_metastore.
quickwit-up:
	$(MAKE) docker-compose-up DOCKER_SERVICES=quickwit

quickwit-down:
	docker compose -p quickwit stop quickwit
	docker compose -p quickwit rm -f quickwit

quickwit-logs:
	docker compose -p quickwit logs -f quickwit

# Path to the central Python venv at frameworks/.venv. Override on the command
# line if needed: `make quickwit-ingest PYTHON=/some/other/python`.
PYTHON ?= ../.venv/bin/python

# Create the two demo indexes from src/index-*.yaml against a running Quickwit.
# Idempotent in spirit: a second run gets HTTP 400 (already exists) and the
# loop continues. Inspect the printed status codes to confirm.
quickwit-create-indexes:
	@for cfg in src/index-articles.yaml src/index-app-logs.yaml \
	            src/index-users.yaml src/index-orders.yaml src/index-products.yaml; do \
		printf "POST %s -> " "$$cfg"; \
		curl -sS -o /tmp/qw-create.out -w "HTTP %{http_code}\n" \
		  -X POST http://localhost:7280/api/v1/indexes \
		  -H "Content-Type: application/yaml" \
		  --data-binary @$$cfg; \
	done

# Stream the NDJSON.gz from OCI into Quickwit's ingest REST endpoint for both
# indexes. Requires OCI_REGION / OCI_BUCKET / OCI_ACCESS_KEY /
# OCI_SECRET_ACCESS_KEY to be exported, and the central venv to have boto3.
quickwit-ingest:
	@test -x "$(PYTHON)" || (echo "Python not executable at $(PYTHON). Set PYTHON=... or activate frameworks/.venv"; exit 1)
	$(PYTHON) src/ingest_to_quickwit.py

# Ray-based parallel ingest. Reads .zip files of NDJSON from `INPUT` (an OCI
# prefix or a local directory) and routes the rows through `NUM_INDEXERS`
# Ray actors that POST to /api/v1/$(INDEX)/ingest. Requires `pip install ray`
# in the central venv plus the same OCI_* env vars as quickwit-ingest.
#
# Required:
#   INDEX=<index_id>
# Optional overrides (defaults shown):
#   INPUT=s3://$$OCI_BUCKET/enrichedsi/
#   NUM_INDEXERS=16
#   BATCH_SIZE=16          (zip files per reader task)
#   INGEST_CHUNK=1000      (rows per /ingest POST)
#   QW=http://localhost:7280
#
# Examples:
#   make quickwit-ray-ingest INDEX=enrichedsi
#   make quickwit-ray-ingest INDEX=enrichedsi INPUT=/tmp/enrichedsi NUM_INDEXERS=8
INPUT ?= s3://$(OCI_BUCKET)/enrichedsi/$(INDEX)/
NUM_INDEXERS ?= 16
BATCH_SIZE ?= 16
INGEST_CHUNK ?= 1000

quickwit-ray-ingest:
	@test -x "$(PYTHON)" || (echo "Python not executable at $(PYTHON). Set PYTHON=... or activate frameworks/.venv"; exit 1)
	@test -n "$(INDEX)" || (echo "ERROR: INDEX=<index_id> is required (e.g. make quickwit-ray-ingest INDEX=enrichedsi)"; exit 2)
	$(PYTHON) src/ray_indexer.py \
	    --input "$(INPUT)" \
	    --index "$(INDEX)" \
	    --quickwit "$(QW)" \
	    --num-indexers $(NUM_INDEXERS) \
	    --batch-size $(BATCH_SIZE) \
	    --ingest-chunk $(INGEST_CHUNK)

# Ray-based Parquet → NDJSON.zip transcoder. Reads .parquet files for
# table $(TABLE) (default location: s3://$$OCI_BUCKET/parquet/$(TABLE)/)
# and emits .zip files of NDJSON to $(PARQUET_OUTPUT) (default:
# s3://$$OCI_BUCKET/enrichedsi/) — the layout that quickwit-ray-ingest
# consumes. Pass --columns to project; otherwise all columns are emitted.
#
# Required:
#   TABLE=<table_name>
# Optional overrides (defaults shown):
#   PARQUET_INPUT=s3://$$OCI_BUCKET/parquet/$(TABLE)/
#   PARQUET_OUTPUT=s3://$$OCI_BUCKET/enrichedsi/
#   COLUMNS=col1,col2,col3        (default: all columns)
#   IN_FLIGHT=64
#
# Examples:
#   make quickwit-parquet-to-jsonl TABLE=docref
#   make quickwit-parquet-to-jsonl TABLE=docref \
#       COLUMNS=DOCUMENTREFERENCE_SO_ID,DOCUMENTREFERENCE_VALUE
# Iceberg layout written by ../polaris/src/generate_iceberg_tables.py via
# Polaris (warehouse=polaris-iceberg, namespace=demo). Polaris uses
# Iceberg-native namespace naming (<namespace>/<table>/), not the Hive-style
# <namespace>.db/<table>/ that PyIceberg's SqlCatalog produces. Path:
#   s3://$$OCI_BUCKET/polaris-iceberg/demo/<table>/data/<uuid>.parquet
PARQUET_INPUT ?= s3://$(OCI_BUCKET)/polaris-iceberg/demo/$(TABLE)/data/
# Per-table output prefix so multiple tables don't mix in one folder.
PARQUET_OUTPUT ?= s3://$(OCI_BUCKET)/enrichedsi/$(TABLE)/
COLUMNS ?=
IN_FLIGHT ?= 64

quickwit-parquet-to-jsonl:
	@test -x "$(PYTHON)" || (echo "Python not executable at $(PYTHON). Set PYTHON=... or activate frameworks/.venv"; exit 1)
	@test -n "$(TABLE)" || (echo "ERROR: TABLE=<table_name> is required (e.g. make quickwit-parquet-to-jsonl TABLE=docref)"; exit 2)
	$(PYTHON) src/parquet_to_jsonl.py \
	    --input "$(PARQUET_INPUT)" \
	    --output "$(PARQUET_OUTPUT)" \
	    $(if $(COLUMNS),--columns "$(COLUMNS)",) \
	    --in-flight $(IN_FLIGHT)

# Run the smoke-test / demo query suite against the running node.
# Override host: `make quickwit-test QW=http://other-host:7280`.
QW ?= http://localhost:7280
quickwit-test:
	QW="$(QW)" ./src/test_quickwit.sh

docker-rm-postgres-volume:
	docker volume rm quickwit_postgres_data

docker-rm-volumes:
	docker volume rm quickwit_azurite_data quickwit_fake_gcs_server_data quickwit_grafana_conf quickwit_grafana_data quickwit_localstack_data quickwit_postgres_data

doc:
	@$(MAKE) -C $(QUICKWIT_SRC) doc

fmt:
	@$(MAKE) -C $(QUICKWIT_SRC) fmt

fix:
	@$(MAKE) -C $(QUICKWIT_SRC) fix

migrations-lint:
	@$(MAKE) -C $(QUICKWIT_SRC) migrations-lint

typos:
	typos

# Usage:
# `make test-all` starts the Docker services and runs all the tests.
# `make -k test-all docker-compose-down`, tears down the Docker services after running all the tests.
test-all: docker-compose-up
	@$(MAKE) -C $(QUICKWIT_SRC) test-all

test-failpoints:
	@$(MAKE) -C $(QUICKWIT_SRC) test-failpoints

# This will build and push all custom cross images for cross-compilation.
# You will need to login into Docker Hub with the `quickwit` account.
IMAGE_TAGS = x86_64-unknown-linux-gnu aarch64-unknown-linux-gnu x86_64-unknown-linux-musl aarch64-unknown-linux-musl

.PHONY: cross-images
cross-images:
	@for tag in ${IMAGE_TAGS}; do \
		docker build --tag quickwit/cross:$$tag --file ./build/cross-images/$$tag.dockerfile ./build/cross-images; \
		docker push quickwit/cross:$$tag; \
	done

# TODO: to be replaced by https://github.com/quickwit-oss/quickwit/issues/237
.PHONY: build
build: build-ui
	$(MAKE) -C $(QUICKWIT_SRC) build

# Usage:
# `BINARY_FILE=path/to/quickwit/binary BINARY_VERSION=0.1.0 ARCHIVE_NAME=quickwit make archive`
# - BINARY_FILE: Path of the quickwit binary file.
# - BINARY_VERSION: Version of the quickwit binary.
# - ARCHIVE_NAME: Name of the resulting archive file (without extension).
.PHONY: archive
archive:
	@echo "Archiving release binary & assets"
	@mkdir -p "./quickwit-${BINARY_VERSION}/config"
	@mkdir -p "./quickwit-${BINARY_VERSION}/qwdata"
	@cp ./config/quickwit.yaml "./quickwit-${BINARY_VERSION}/config"
	@cp ./LICENSE "./quickwit-${BINARY_VERSION}"
	@cp "${BINARY_FILE}" "./quickwit-${BINARY_VERSION}"
	@tar -czf "${ARCHIVE_NAME}.tar.gz" "./quickwit-${BINARY_VERSION}"
	@rm -rf "./quickwit-${BINARY_VERSION}"

workspace-deps-tree:
	$(MAKE) -C $(QUICKWIT_SRC) workspace-deps-tree

.PHONY: build-rustdoc
build-rustdoc:
	$(MAKE) -C $(QUICKWIT_SRC) build-rustdoc

.PHONY: build-ui
build-ui:
	$(MAKE) -C $(QUICKWIT_SRC) build-ui
