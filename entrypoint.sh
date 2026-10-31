#!/bin/sh
set -e

# Token store the server reads (same default as garmin_mcp.token_utils).
TOKEN_DIR="${GARMINTOKENS:-${HOME:-/root}/.garminconnect}"
TOKEN_FILE="$TOKEN_DIR/garmin_tokens.json"
SEED_HASH_FILE="$TOKEN_DIR/.seeded_from_env"
mkdir -p "$TOKEN_DIR"

# GARMIN_TOKENS_JSON seeds the token store. Garmin rotates the refresh token
# whenever the server refreshes its access token, and the server writes the
# new tokens back to $TOKEN_FILE, so after a while the copy in the variable no
# longer works. Mount a volume on $TOKEN_DIR so the saved tokens survive
# restarts and redeploys, and only overwrite them when the variable itself has
# changed (i.e. after a fresh garmin-mcp-auth).
if [ -n "$GARMIN_TOKENS_JSON" ]; then
  env_hash=$(printf '%s' "$GARMIN_TOKENS_JSON" | sha256sum | cut -d' ' -f1)
  seeded_hash=$(cat "$SEED_HASH_FILE" 2>/dev/null || true)
  if [ ! -s "$TOKEN_FILE" ] || [ "$env_hash" != "$seeded_hash" ]; then
    printf '%s' "$GARMIN_TOKENS_JSON" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    printf '%s\n' "$env_hash" > "$SEED_HASH_FILE"
    echo "entrypoint: seeded Garmin tokens from GARMIN_TOKENS_JSON" >&2
  else
    echo "entrypoint: using the Garmin tokens saved in $TOKEN_DIR" >&2
  fi
fi

exec garmin-mcp
