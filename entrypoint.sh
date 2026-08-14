#!/bin/sh
set -e
mkdir -p /root/.garminconnect
if [ -n "$GARMIN_TOKENS_JSON" ]; then
  printf '%s' "$GARMIN_TOKENS_JSON" > /root/.garminconnect/garmin_tokens.json
fi
exec garmin-mcp
