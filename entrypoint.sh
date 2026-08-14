#!/bin/sh
set -e
mkdir -p /root/.garminconnect
if [ -n "$GARMIN_TOKENS_JSON" ]; then
  echo "$GARMIN_TOKENS_JSON" | python3 -c "
import json, sys
data = json.load(sys.stdin)
with open('/root/.garminconnect/oauth1_token.json', 'w') as f:
    json.dump(data['oauth1'], f)
with open('/root/.garminconnect/oauth2_token.json', 'w') as f:
    json.dump(data['oauth2'], f)
"
fi
exec garmin-mcp
