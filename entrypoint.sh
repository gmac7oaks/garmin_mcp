#!/bin/sh
set -e
mkdir -p /root/.garminconnect
if [ -n "$GARMIN_TOKENS_JSON" ]; then
  echo "$GARMIN_TOKENS_JSON" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for filename, content in data.items():
    with open(f'/root/.garminconnect/{filename}', 'w') as f:
        json.dump(content, f)
"
fi
exec garmin-mcp
