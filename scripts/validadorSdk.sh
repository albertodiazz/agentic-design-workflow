DATA_URL="$(cat /tmp/penpot_debug/validator_export.png.data_url.txt)"

jq -n \
  --arg model "${MISTRAL_VISION_MODEL:-mistral-large-2512}" \
  --arg img "$DATA_URL" \
  '{
    model: $model,
    messages: [
      {
        role: "user",
        content: [
          {
            type: "text",
            text: "Describe esta imagen brevemente. Responde solo JSON: {\"ok\": true, \"summary\": \"...\"}"
          },
          {
            type: "image_url",
            image_url: $img
          }
        ]
      }
    ],
    response_format: {
      type: "json_object"
    },
    max_tokens: 200,
    temperature: 0
  }' > /tmp/mistral_penpot_image_test.json

curl -v \
  --connect-timeout 10 \
  --max-time 180 \
  -w "\nHTTP=%{http_code} DNS=%{time_namelookup}s CONNECT=%{time_connect}s STARTTRANSFER=%{time_starttransfer}s TOTAL=%{time_total}s SIZE_UPLOAD=%{size_upload} SIZE_DOWNLOAD=%{size_download}\n" \
  https://api.mistral.ai/v1/chat/completions \
  -H "Authorization: Bearer $MISTRAL_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/mistral_penpot_image_test.json
