#!/usr/bin/env bash
set -u
T_A_TOKEN="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiJlYzk1MzZlZTc4YTM0ZTQ3ODdjMGMwNmUzYTA4NzMzMyIsInN1YiI6IjNmODhhMjMzLTRkMTEtNTBlNi05MjZiLWUwZGRkMjgzOGMwYyIsInRlbmFudF9pZCI6InRlbmFudF9hIiwicm9sZSI6ImNvbnN1bWVyIiwiaWF0IjoxNzg5Mzg3OTE5LCJleHAiOjE3OTE5Nzk5MTksInR5cGUiOiJkZW1vX2FjY2VzcyJ9.LQxR6FwgkiptHQ6zEWvXozrFYP_bJeVkdLixjU4cU5c"
SUF=$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')
THREAD_ID2="tenant_a:${SUF}"
echo "=== 创建 thread ${THREAD_ID2} ==="
CREATE_HTTP=$(curl -sS -o /dev/null -w '%{http_code}' -X POST http://localhost:8000/api/conversations \
  -H "X-Tenant-Id: tenant_a" -H "Authorization: Bearer ${T_A_TOKEN}" \
  -H "Content-Type: application/json" -d '{"title":"debug_stream2"}')
echo "thread created HTTP=${CREATE_HTTP}"
echo "=== 抓 SSE 内容 -> /tmp/sse_raw.txt ==="
set +e
curl -sS --max-time 30 -N -X POST "http://localhost:8000/api/agent/conversations/${THREAD_ID2}/stream" \
  -H "X-Tenant-Id: tenant_a" -H "Authorization: Bearer ${T_A_TOKEN}" \
  -H "Content-Type: application/json" -H "Accept: text/event-stream" \
  -d '{"text":"你好"}' -D /tmp/sse_headers.txt > /tmp/sse_raw.txt
EC=$?
set -e
echo "curl exit=${EC}, body_bytes=$(wc -c < /tmp/sse_raw.txt)"
echo ""
echo "=== HTTP response headers ==="
cat /tmp/sse_headers.txt
echo ""
echo "=== SSE body (cat -v 可视化) ==="
LANG=C cat -v /tmp/sse_raw.txt
echo ""
echo "=== 解析每帧（按 \n\n 切分，统计 reply_chunk 数量和长度）==="
python3 << 'PYEOF'
from pathlib import Path
raw = Path("/tmp/sse_raw.txt").read_bytes().decode("utf-8", errors="replace")
frames = [f for f in raw.split("\n\n") if f.strip()]
rc_count = 0
rc_total_len = 0
reply_text = ""
reply_len = None
done_seen = False
start_seen = False
node_evts = []
err_evts = []
for i, f in enumerate(frames):
    lines = [ln for ln in f.split("\n") if ln.strip()]
    data_lines = [ln[6:] if ln.startswith("data: ") else ln for ln in lines if ln.startswith("data:") or ln.startswith('data:') or ln.startswith('{')]
    import json
    text = "\n".join(data_lines)
    try:
        obj = json.loads(text)
    except Exception:
        print(f"[frame {i}] 非JSON: {text[:120]!r}")
        continue
    t = obj.get("type")
    if t == "start": start_seen = True
    elif t == "reply_chunk":
        rc_count += 1
        rc_total_len += len(obj.get("text","") or "")
    elif t == "reply":
        reply_text = obj.get("text","") or ""
        reply_len = len(reply_text)
    elif t == "done":
        done_seen = True
    elif t == "error":
        err_evts.append(obj)
    elif t in ("node_start","node_end"):
        node_evts.append(obj)
print(f"start: {start_seen}  |  done: {done_seen}  |  node_events: {len(node_evts)}  |  errors: {len(err_evts)}")
print(f"reply_chunk_count = {rc_count}  |  reply_chunk_total_chars = {rc_total_len}")
print(f"reply.final_len = {reply_len}")
if rc_count == 0:
    print(f"[!!] NO STREAMING CHUNKS AT ALL")
if reply_len is not None and reply_len < 20:
    print(f"[!!] final reply is too short: {reply_text!r}")
for e in err_evts:
    print(f"[error evt]: {e}")
for n in node_evts:
    print(f"[node] {n}")
PYEOF
