#!/usr/bin/env bash
#
# Demo / smoke-test queries against the Quickwit node started by `make
# quickwit-up`, populated by `make quickwit-create-indexes` + `make
# quickwit-ingest`.
#
# Indexes:  articles  (~2000 docs)  +  app_logs  (~20000 docs)
#
# Run:
#   ./src/test_quickwit.sh
#
# Override the host:
#   QW=http://other-host:7280 ./src/test_quickwit.sh

set -euo pipefail

QW="${QW:-http://localhost:7280}"

# ---- formatting helpers ---------------------------------------------------

if [[ -t 1 ]]; then
  bold=$'\033[1m'; dim=$'\033[2m'; green=$'\033[32m'; red=$'\033[31m'; reset=$'\033[0m'
else
  bold=""; dim=""; green=""; red=""; reset=""
fi

section() { printf '\n%s== %s ==%s\n' "$bold" "$1" "$reset"; }
step()    { printf '%s>>> %s%s\n' "$dim" "$1" "$reset"; }
note()    { printf '%s    %s%s\n' "$dim" "$1" "$reset"; }

pp() {
  # Pretty-print JSON via jq if available, else raw.
  local filter="${1:-.}"
  if command -v jq >/dev/null 2>&1; then
    jq "$filter"
  else
    cat
  fi
}

# Cross-platform: "epoch seconds N days ago, UTC". Mac date uses -v, GNU
# date uses -d. Try Mac first, fall back to GNU.
days_ago_epoch() {
  local n=$1
  if out=$(date -u -v-"${n}"d +%s 2>/dev/null); then
    printf '%s\n' "$out"
    return 0
  fi
  date -u -d "${n} days ago" +%s
}
now_epoch() { date -u +%s; }

# GET wrapper: curl -G + --data-urlencode handles encoding for us so the
# query strings in this script stay readable.
qq_get() {
  local path=$1; shift
  curl -sS -G "${QW}${path}" "$@"
}

qq_post() {
  local path=$1; local body=$2
  curl -sS -X POST "${QW}${path}" -H "Content-Type: application/json" --data "$body"
}

# ---- preflight ------------------------------------------------------------

section "Preflight"

step "Quickwit version"
qq_get /api/v1/version | pp '.'

for idx in articles app_logs; do
  step "Confirm index '$idx' is registered"
  if ! body=$(qq_get "/api/v1/indexes/$idx" 2>/dev/null) || [[ -z "$body" ]]; then
    printf '%sERROR%s: index %s not found at %s\n' "$red" "$reset" "$idx" "$QW" >&2
    printf '       run: make quickwit-create-indexes && make quickwit-ingest\n' >&2
    exit 1
  fi
  printf '%s' "$body" | pp '{
    index_id: .index_config.index_id,
    num_fields: (.index_config.doc_mapping.field_mappings | length),
    timestamp_field: .index_config.doc_mapping.timestamp_field,
    default_search_fields: .index_config.search_settings.default_search_fields
  }'
done

# ---- Smoke ----------------------------------------------------------------

section "Doc counts (smoke test)"

step "articles total docs"
qq_get /api/v1/articles/search --data-urlencode "query=*" --data-urlencode "max_hits=0" | pp '.num_hits'

step "app_logs total docs"
qq_get /api/v1/app_logs/search --data-urlencode "query=*" --data-urlencode "max_hits=0" | pp '.num_hits'

step "split count per index (visible after ingest)"
qq_get /api/v1/indexes/articles/splits | pp 'length'
qq_get /api/v1/indexes/app_logs/splits | pp 'length'

# ---- Articles: full-text & faceting --------------------------------------

section "Articles — full-text & faceting"

step "Default-field search (title, body, tags) for the word 'system'"
qq_get /api/v1/articles/search \
  --data-urlencode "query=system" \
  --data-urlencode "max_hits=3" \
  | pp '.hits[] | {title, category, tags, language}'

step "Field-targeted: title contains 'data'"
qq_get /api/v1/articles/search \
  --data-urlencode "query=title:data" \
  --data-urlencode "max_hits=3" \
  | pp '.hits[] | {title, author, published_at}'

step "Boolean: business + English + word 'market' anywhere"
qq_get /api/v1/articles/search \
  --data-urlencode "query=category:business AND language:en AND market" \
  --data-urlencode "max_hits=3" \
  | pp '.hits[] | {title, category, language, source}'

step "Faceted browse: technology category, newest first"
note "Quickwit sort_by is inverted vs ES/JSON:API: bare field = Desc, +field = Desc,"
note "-field = Asc. Newest-first is therefore 'published_at' (no prefix) or '+published_at'."
qq_get /api/v1/articles/search \
  --data-urlencode "query=category:technology" \
  --data-urlencode "max_hits=5" \
  --data-urlencode "sort_by=published_at" \
  | pp '.hits[] | {published_at, title}'

# ---- App logs: filters, ranges, types -------------------------------------

section "App logs — filters, ranges, IP types"

step "All ERROR-level (≈4% of 20k)"
qq_get /api/v1/app_logs/search \
  --data-urlencode "query=level:ERROR" \
  --data-urlencode "max_hits=0" \
  | pp '.num_hits'

step "ERRORs in checkout-api specifically"
qq_get /api/v1/app_logs/search \
  --data-urlencode "query=level:ERROR AND service:checkout-api" \
  --data-urlencode "max_hits=5" \
  | pp '.hits[] | {timestamp, service, status_code, message}'

step "All 5xx responses"
qq_get /api/v1/app_logs/search \
  --data-urlencode "query=status_code:>=500" \
  --data-urlencode "max_hits=5" \
  | pp '.hits[] | {service, status_code, http_method, http_path, message}'

step "Slow requests (>1s) — exercises numeric range on a fast field"
qq_get /api/v1/app_logs/search \
  --data-urlencode "query=latency_ms:>1000" \
  --data-urlencode "max_hits=5" \
  | pp '.hits[] | {service, latency_ms, http_path, status_code}'

step "IP type query — count of all logs (full IPv4 range)"
qq_get /api/v1/app_logs/search \
  --data-urlencode "query=client_ip:[0.0.0.0 TO 255.255.255.255]" \
  --data-urlencode "max_hits=0" \
  | pp '.num_hits'

# ---- Time-range pruning (metastore-level filtering) -----------------------

section "Time-range pruning"
note "Quickwit prunes splits whose time_range doesn't overlap *before* reading any object-storage bytes."

START=$(days_ago_epoch 7)
END=$(now_epoch)

step "ERRORs in the last 7 days (start=$START end=$END)"
qq_get /api/v1/app_logs/search \
  --data-urlencode "query=level:ERROR" \
  --data-urlencode "start_timestamp=$START" \
  --data-urlencode "end_timestamp=$END" \
  --data-urlencode "max_hits=0" \
  | pp '{num_hits, elapsed_time_micros}'

START=$(days_ago_epoch 30)
step "Articles published in the last 30 days"
qq_get /api/v1/articles/search \
  --data-urlencode "query=*" \
  --data-urlencode "start_timestamp=$START" \
  --data-urlencode "end_timestamp=$END" \
  --data-urlencode "max_hits=0" \
  | pp '{num_hits, elapsed_time_micros}'

# ---- Aggregations (Elasticsearch-compatible) ------------------------------

section "Aggregations"

step "Distribution of log levels (terms agg)"
qq_post /api/v1/app_logs/search '{
  "query": "*",
  "max_hits": 0,
  "aggs": {
    "by_level": { "terms": { "field": "level", "size": 10 } }
  }
}' | pp '.aggregations.by_level.buckets'

step "Errors per service — \"where are the fires?\""
qq_post /api/v1/app_logs/search '{
  "query": "level:ERROR",
  "max_hits": 0,
  "aggs": {
    "by_service": { "terms": { "field": "service", "size": 20 } }
  }
}' | pp '.aggregations.by_service.buckets'

step "p50 / p95 / p99 latency by HTTP method (nested agg)"
qq_post /api/v1/app_logs/search '{
  "query": "*",
  "max_hits": 0,
  "aggs": {
    "by_method": {
      "terms": { "field": "http_method", "size": 10 },
      "aggs": {
        "latency_pct": {
          "percentiles": { "field": "latency_ms", "percents": [50, 95, 99] }
        }
      }
    }
  }
}' | pp '.aggregations.by_method.buckets'

step "Errors per day, last 14 days (date_histogram)"
qq_post /api/v1/app_logs/search '{
  "query": "level:ERROR",
  "max_hits": 0,
  "aggs": {
    "errors_daily": {
      "date_histogram": {
        "field": "timestamp",
        "fixed_interval": "1d"
      }
    }
  }
}' | pp '.aggregations.errors_daily.buckets'

step "Article tag cloud — top 20 tags"
qq_post /api/v1/articles/search '{
  "query": "*",
  "max_hits": 0,
  "aggs": {
    "popular_tags": { "terms": { "field": "tags", "size": 20 } }
  }
}' | pp '.aggregations.popular_tags.buckets'

# ---- Showcase: combined query --------------------------------------------

section "Showcase — combined query"
note "boolean text + time range pruning + terms aggregation, all in one request."

START=$(days_ago_epoch 1)
END=$(now_epoch)

qq_post /api/v1/app_logs/search "{
  \"query\": \"level:ERROR AND service:checkout-api\",
  \"start_timestamp\": $START,
  \"end_timestamp\": $END,
  \"max_hits\": 0,
  \"aggs\": {
    \"by_status\": { \"terms\": { \"field\": \"status_code\", \"size\": 10 } }
  }
}" | pp '{
  total: .num_hits,
  elapsed_time_micros: .elapsed_time_micros,
  breakdown: .aggregations.by_status.buckets
}'

printf '\n%sAll queries completed.%s\n' "$green" "$reset"
