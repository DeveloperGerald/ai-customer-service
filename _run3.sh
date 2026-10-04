#!/usr/bin/env bash
set -ue
T_A_TOKEN="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiJlYzk1MzZlZTc4YTM0ZTQ3ODdjMGMwNmUzYTA4NzMzMyIsInN1YiI6IjNmODhhMjMzLTRkMTEtNTBlNi05MjZiLWUwZGRkMjgzOGMwYyIsInRlbmFudF9pZCI6InRlbmFudF9hIiwicm9sZSI6ImNvbnN1bWVyIiwiaWF0IjoxNzg5Mzg3OTE5LCJleHAiOjE3OTE5Nzk5MTksInR5cGUiOiJkZW1vX2FjY2VzcyJ9.LQxR6FwgkiptHQ6zEWvXozrFYP_bJeVkdLixjU4cU5c"
THREAD_ID="tenant_a:761f005375174c6293e0c170f8debcf6"
URL="http://localhost:8000/api/agent/conversations/${THREAD_ID}/stream"
curl -sS --no-buffer --max-time 60 -N -X POST "$URL" \
  -H "X-Tenant-Id: tenant_a" \
  -H "Authorization: Bearer ${T_A_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"text":"你好"}' > /tmp/sse_raw5.txt
EC=$?
echo "curl exit=$EC size_bytes=$(wc -c < /tmp/sse_raw5.txt)"
