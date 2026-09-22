#!/usr/bin/env bash
# Deploy socialq to Appwrite Functions. §11.
#
# Three functions, same code, different COMMAND and schedule. Appwrite uploads
# a directory, so this assembles one containing the entrypoint, the socialq
# package and its requirements -- there is no "install my repo" step to lean on.
#
#   ./scripts/deploy_appwrite.sh            deploy all three
#   ./scripts/deploy_appwrite.sh worker     deploy one
#
# Reads APPWRITE_* and the runtime secrets from .env.

set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a

: "${APPWRITE_ENDPOINT:?}" "${APPWRITE_PROJECT_ID:?}" "${APPWRITE_API_KEY:?}"
: "${DATABASE_URL:?}" "${SOCIALQ_SECRET_KEY:?}"

BUILD=.appwrite-build
RUNTIME=python-3.12

# The secrets a job needs at runtime. Note the credential tokens are NOT here:
# they live encrypted in the database (§8.2.1), and SOCIALQ_SECRET_KEY is what
# unlocks them. That is the whole point of the indirection.
VARS=(
  "DATABASE_URL=$DATABASE_URL"
  "SOCIALQ_SECRET_KEY=$SOCIALQ_SECRET_KEY"
  "R2_ACCOUNT_ID=${R2_ACCOUNT_ID:-}"
  "R2_ACCESS_KEY_ID=${R2_ACCESS_KEY_ID:-}"
  "R2_SECRET_ACCESS_KEY=${R2_SECRET_ACCESS_KEY:-}"
  "R2_BUCKET=${R2_BUCKET:-}"
  "R2_PUBLIC_BASE_URL=${R2_PUBLIC_BASE_URL:-}"
)

assemble() {
  rm -rf "$BUILD"
  mkdir -p "$BUILD"
  cp appwrite/main.py "$BUILD/main.py"
  cp -r socialq "$BUILD/socialq"
  find "$BUILD" -name '__pycache__' -type d -prune -exec rm -rf {} +
  # Pinned to what the tests run against, not resolved fresh at deploy time.
  cat > "$BUILD/requirements.txt" <<'EOF'
psycopg[binary]>=3.2
httpx>=0.27
boto3>=1.34
cryptography>=42.0
EOF
  echo "assembled $(du -sh "$BUILD" | cut -f1) in $BUILD"
}

configure() {
  local id="$1" name="$2" schedule="$3" command="$4" timeout="$5"

  if appwrite functions get --function-id "$id" >/dev/null 2>&1; then
    appwrite functions update --function-id "$id" --name "$name" \
      --runtime "$RUNTIME" --entrypoint main.py --timeout "$timeout" \
      --commands "pip install -r requirements.txt" \
      --schedule "$schedule" --enabled >/dev/null
    echo "  updated $id"
  else
    appwrite functions create --function-id "$id" --name "$name" \
      --runtime "$RUNTIME" --entrypoint main.py --timeout "$timeout" \
      --commands "pip install -r requirements.txt" \
      --schedule "$schedule" --enabled >/dev/null
    echo "  created $id"
  fi

  for pair in "${VARS[@]}" "COMMAND=$command"; do
    local key="${pair%%=*}" value="${pair#*=}"
    [ -z "$value" ] && continue
    # Variable IDs are unique per PROJECT, not per function, so they carry
    # the function name -- otherwise the second function collides with the first.
    local vid
    vid=$(echo -n "$id-$key" | tr 'A-Z_' 'a-z-' | cut -c1-36)
    appwrite functions delete-variable --function-id "$id" \
      --variable-id "$vid" >/dev/null 2>&1 || true
    appwrite functions create-variable --function-id "$id" \
      --variable-id "$vid" --key "$key" --value "$value" --secret true \
      >/dev/null
  done
  echo "  variables set (${#VARS[@]} + COMMAND)"
}

deploy() {
  local id="$1"
  local dep
  dep=$(appwrite functions create-deployment --function-id "$id" \
    --code "$BUILD" --activate true --json | python -c \
    'import sys,json; print(json.load(sys.stdin)["$id"])')
  echo -n "  building $dep "
  for _ in $(seq 1 60); do
    local status
    status=$(appwrite functions get-deployment --function-id "$id" \
      --deployment-id "$dep" --json | python -c \
      'import sys,json; print(json.load(sys.stdin)["status"])')
    case "$status" in
      ready) echo "ready"; return 0 ;;
      failed) echo "FAILED"
              appwrite functions get-deployment --function-id "$id" \
                --deployment-id "$dep" --json | python -c \
                'import sys,json; print(json.load(sys.stdin).get("buildLogs","")[-2000:])'
              return 1 ;;
    esac
    echo -n "."
    sleep 5
  done
  echo " TIMED OUT"; return 1
}

# id | name | schedule | command | timeout seconds
JOBS=(
  "worker|socialq worker|* * * * *|worker|600"
  "reconcile|socialq reconcile|*/10 * * * *|reconcile|900"
  "prune|socialq prune|0 4 * * *|prune|900"
  # No schedule: run it by hand after a deploy to prove the whole chain works
  # from inside the runtime, where the environment differs from a laptop.
  "doctor|socialq doctor||doctor|300"
)

assemble
for job in "${JOBS[@]}"; do
  IFS='|' read -r id name schedule command timeout <<< "$job"
  [ $# -gt 0 ] && [ "$1" != "$id" ] && continue
  echo "$id ($schedule):"
  configure "$id" "$name" "$schedule" "$command" "$timeout"
  deploy "$id"
done

rm -rf "$BUILD"
echo
echo "deployed. check with:  appwrite functions list"
