#!/usr/bin/env bash
#
# Demo / smoke-test queries against the Quickwit node, exercising the three
# tables produced by ../polaris/src/generate_iceberg_tables.py:
#
#   users     — id, name (text), email (raw), address (text), signed_up_at (ts)
#   orders    — id, user_id, amount (f64), status (raw), placed_at (ts)
#   products  — id, name (text), category (raw), price (f64)
#
# Assumed pipeline state (re-run if missing):
#   make quickwit-create-indexes
#   for t in users orders products; do
#     make quickwit-parquet-to-jsonl TABLE=$t
#     make quickwit-ray-ingest      INDEX=$t
#   done
#
# Run:
#   ./test/quickwit_query.sh
#
# Override the host:
#   QW=http://other-host:7280 ./test/quickwit_query.sh

set -euo pipefail

QW="${QW:-http://localhost:7280}"

# ---- formatting helpers ---------------------------------------------------

if [[ -t 1 ]]; then
  bold=$'\033[1m'; dim=$'\033[2m'; green=$'\033[32m'; red=$'\033[31m'
  yellow=$'\033[33m'; reset=$'\033[0m'
else
  bold=""; dim=""; green=""; red=""; yellow=""; reset=""
fi

section() { printf '\n%s== %s ==%s\n' "$bold" "$1" "$reset"; }
step()    { printf '%s>>> %s%s\n' "$dim" "$1" "$reset"; }
warn()    { printf '%sWARN %s%s\n' "$yellow" "$1" "$reset"; }
note()    { printf '%s    %s%s\n' "$dim" "$1" "$reset"; }

pp() {
  local filter="${1:-.}"
  if command -v jq >/dev/null 2>&1; then
    jq "$filter"
  else
    cat
  fi
}

days_ago_epoch() {
  local n=$1
  if out=$(date -u -v-"${n}"d +%s 2>/dev/null); then
    printf '%s\n' "$out"
    return 0
  fi
  date -u -d "${n} days ago" +%s
}
now_epoch() { date -u +%s; }

qq_get()  { local path=$1; shift; curl -sS -G "${QW}${path}" "$@"; }
qq_post() { local path=$1; local body=$2; curl -sS -X POST "${QW}${path}" -H "Content-Type: application/json" --data "$body"; }

count_hits() {
  qq_get "/api/v1/$1/search" --data-urlencode "query=*" --data-urlencode "max_hits=0" \
    | pp '.num_hits' | tr -d '\n'
}

# ---- preflight ------------------------------------------------------------

section "Preflight"

step "Quickwit version"
qq_get /api/v1/version | pp '.'

for idx in users orders products; do
  step "Confirm index '$idx' is registered"
  if ! body=$(qq_get "/api/v1/indexes/$idx" 2>/dev/null) || [[ -z "$body" ]]; then
    printf '%sERROR%s: index %s not found at %s\n' "$red" "$reset" "$idx" "$QW" >&2
    printf '       run: make quickwit-create-indexes\n' >&2
    exit 1
  fi
  printf '%s' "$body" | pp '{
    index_id: .index_config.index_id,
    fields: (.index_config.doc_mapping.field_mappings | length),
    timestamp_field: .index_config.doc_mapping.timestamp_field,
    default_search_fields: .index_config.search_settings.default_search_fields
  }'
done

# Three known indexes — individual vars instead of an associative array so
# this works on macOS's default bash 3.2 (declare -A needs bash 4+).
HITS_USERS=$(count_hits users)
HITS_ORDERS=$(count_hits orders)
HITS_PRODUCTS=$(count_hits products)

# ---- Doc counts -----------------------------------------------------------

section "Doc counts (smoke test)"
printf '  %-10s %s\n' users    "$HITS_USERS"
printf '  %-10s %s\n' orders   "$HITS_ORDERS"
printf '  %-10s %s\n' products "$HITS_PRODUCTS"

empty=()
[[ "$HITS_USERS"    == "0" ]] && empty+=("users")
[[ "$HITS_ORDERS"   == "0" ]] && empty+=("orders")
[[ "$HITS_PRODUCTS" == "0" ]] && empty+=("products")
if (( ${#empty[@]} > 0 )); then
  warn "Indexes with 0 docs: ${empty[*]} — sections that target them will be skipped."
  for idx in "${empty[@]}"; do
    note "  make quickwit-parquet-to-jsonl TABLE=$idx && make quickwit-ray-ingest INDEX=$idx"
  done
fi

# ---- Users ----------------------------------------------------------------

if [[ "$HITS_USERS" != "0" ]]; then
  section "Users — full-text & sorting"

  step "Default-field search (name/email/address) for 'smith'"
  qq_get /api/v1/users/search \
    --data-urlencode "query=smith" \
    --data-urlencode "max_hits=3" \
    | pp '.hits[] | {id, name, email}'

  step "Field-targeted: name:Jennifer"
  qq_get /api/v1/users/search \
    --data-urlencode "query=name:Jennifer" \
    --data-urlencode "max_hits=3" \
    | pp '.hits[] | {id, name, email}'

  step "Newest signups first"
  note "Quickwit sort_by is inverted vs ES/JSON:API: bare/+ field = Desc, -field = Asc."
  qq_get /api/v1/users/search \
    --data-urlencode "query=*" \
    --data-urlencode "max_hits=5" \
    --data-urlencode "sort_by=signed_up_at" \
    | pp '.hits[] | {signed_up_at, name}'

  step "Signups bucketed by 30-day windows"
  qq_post /api/v1/users/search '{
    "query": "*",
    "max_hits": 0,
    "aggs": {
      "by_window": {
        "date_histogram": { "field": "signed_up_at", "fixed_interval": "30d" }
      }
    }
  }' | pp '.aggregations.by_window.buckets'
fi

# ---- Orders ---------------------------------------------------------------

if [[ "$HITS_ORDERS" != "0" ]]; then
  section "Orders — range filters & status facets"

  step "Big-ticket orders: amount:[500 TO 999]"
  qq_get /api/v1/orders/search \
    --data-urlencode "query=amount:[500 TO 999]" \
    --data-urlencode "max_hits=3" \
    | pp '.hits[] | {id, user_id, amount, status, placed_at}'

  step "All paid orders (count)"
  qq_get /api/v1/orders/search \
    --data-urlencode "query=status:paid" \
    --data-urlencode "max_hits=0" \
    | pp '.num_hits'

  step "Distribution of order statuses"
  qq_post /api/v1/orders/search '{
    "query": "*",
    "max_hits": 0,
    "aggs": {
      "by_status": { "terms": { "field": "status", "size": 10 } }
    }
  }' | pp '.aggregations.by_status.buckets'

  step "Avg / min / max amount per status (nested stats agg)"
  qq_post /api/v1/orders/search '{
    "query": "*",
    "max_hits": 0,
    "aggs": {
      "by_status": {
        "terms": { "field": "status", "size": 10 },
        "aggs": {
          "amount_stats": { "stats": { "field": "amount" } }
        }
      }
    }
  }' | pp '.aggregations.by_status.buckets'
fi

# ---- Products -------------------------------------------------------------

if [[ "$HITS_PRODUCTS" != "0" ]]; then
  section "Products — text on name, range on price, facet on category"

  step "Mid-range products: price:[100 TO 200]"
  qq_get /api/v1/products/search \
    --data-urlencode "query=price:[100 TO 200]" \
    --data-urlencode "max_hits=3" \
    | pp '.hits[] | {id, name, category, price}'

  step "Distribution of product categories"
  qq_post /api/v1/products/search '{
    "query": "*",
    "max_hits": 0,
    "aggs": {
      "by_category": { "terms": { "field": "category", "size": 10 } }
    }
  }' | pp '.aggregations.by_category.buckets'

  step "Price p50 / p90 / p99 per category"
  qq_post /api/v1/products/search '{
    "query": "*",
    "max_hits": 0,
    "aggs": {
      "by_category": {
        "terms": { "field": "category", "size": 10 },
        "aggs": {
          "price_pct": { "percentiles": { "field": "price", "percents": [50, 90, 99] } }
        }
      }
    }
  }' | pp '.aggregations.by_category.buckets'
fi

# ---- Time-range pruning ---------------------------------------------------

if [[ "$HITS_USERS" != "0" || "$HITS_ORDERS" != "0" ]]; then
  section "Time-range pruning"
  note "Quickwit prunes splits whose time_range doesn't overlap *before* reading any object-storage bytes."

  END_NOW=$(now_epoch)

  if [[ "$HITS_USERS" != "0" ]]; then
    START_30=$(days_ago_epoch 30)
    step "Users signed up in the last 30 days (start=$START_30 end=$END_NOW)"
    qq_get /api/v1/users/search \
      --data-urlencode "query=*" \
      --data-urlencode "start_timestamp=$START_30" \
      --data-urlencode "end_timestamp=$END_NOW" \
      --data-urlencode "max_hits=0" \
      | pp '{num_hits, elapsed_time_micros}'
  fi

  if [[ "$HITS_ORDERS" != "0" ]]; then
    START_90=$(days_ago_epoch 90)
    step "Orders placed in the last 90 days (start=$START_90 end=$END_NOW)"
    qq_get /api/v1/orders/search \
      --data-urlencode "query=*" \
      --data-urlencode "start_timestamp=$START_90" \
      --data-urlencode "end_timestamp=$END_NOW" \
      --data-urlencode "max_hits=0" \
      | pp '{num_hits, elapsed_time_micros}'
  fi
fi

# ---- Showcase -------------------------------------------------------------

if [[ "$HITS_ORDERS" != "0" ]]; then
  section "Showcase — boolean + range + time pruning + agg in one request"

  START=$(days_ago_epoch 365)
  END=$(now_epoch)

  qq_post /api/v1/orders/search "{
    \"query\": \"status:paid AND amount:>500\",
    \"start_timestamp\": $START,
    \"end_timestamp\": $END,
    \"max_hits\": 0,
    \"aggs\": {
      \"top_buyers\": { \"terms\": { \"field\": \"user_id\", \"size\": 10 } }
    }
  }" | pp '{
    total: .num_hits,
    elapsed_time_micros: .elapsed_time_micros,
    top_buyers: .aggregations.top_buyers.buckets
  }'
fi

printf '\n%sAll queries completed.%s\n' "$green" "$reset"
