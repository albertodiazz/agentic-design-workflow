curl -v \
  --connect-timeout 10 \
  --max-time 180 \
  -w "\nHTTP=%{http_code} STARTTRANSFER=%{time_starttransfer}s TOTAL=%{time_total}s SIZE_UPLOAD=%{size_upload}\n" \
  https://api.mistral.ai/v1/chat/completions \
  -H "Authorization: Bearer $MISTRAL_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/penpot_debug/validator_payload_debug.json
