#!/usr/bin/env bash
set -ueo pipefail
T_A_TOKEN="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiJlYzk1MzZlZTc4YTM0ZTQ3ODdjMGMwNmUzYTA4NzMzMyIsInN1YiI6IjNmODhhMjMzLTRkMTEtNTBlNi05MjZiLWUwZGRkMjgzOGMwYyIsInRlbmFudF9pZCI6InRlbmFudF9hIiwicm9sZSI6ImNvbnN1bWVyIiwiaWF0IjoxNzg5Mzg3OTE5LCJleHAiOjE3OTE5Nzk5MTksInR5cGUiOiJkZW1vX2FjY2VzcyJ9.LQxR6FwgkiptHQ6zEWvXozrFYP_bJeVkdLixjU4cU5c"
THREAD_ID="tenant_a:761f005375174c6293e0c170f8debcf6"
echo "=== 使用 thread_id = ${THREAD_ID} （HTTP GET 校验合法性）==="
CHECK=$(curl -sS -o /tmp/check_thread.json -w '%{http_code}' \
  -X GET "http://localhost:8000/api/conversations/${THREAD_ID}/messages?limit=2" \
  -H "X-Tenant-Id: tenant_a" -H "Authorization: Bearer ${T_A_TOKEN}")
echo "thread GET HTTP=${CHECK}; /tmp/check_thread.json 前 80 bytes = $(head -c 80 /tmp/check_thread.json 2>/dev/null | tr -d '\n')"
[ "${CHECK}" != "200" ] && { echo "THREAD 不存在，ABORT."; exit 2; }
echo "=== 开始 SSE（curl --no-buffer） ==="
set +e
curl -sS --no-buffer --max-time 90 -N -X POST \
  "http://localhost:8000/api/agent/conversations/${THREAD_ID}/stream" \
  -H "X-Tenant-Id: tenant_a" \
  -H "Authorization: Bearer ${T_A_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"text":"你好"}' \
  -D /tmp/sse_headers.txt > /tmp/sse_raw.txt
EC=$?
set -e
echo ""
echo "=== RESULT ==="
echo "curl exit=${EC}, body_bytes=$(wc -c < /tmp/sse_raw.txt)"
echo ""
echo "=== HTTP response headers ==="
cat /tmp/sse_headers.txt
echo ""
echo "=== SSE body (cat -v; 每行前面加时间戳提示) ==="
LANG=C awk '{print strftime("%M:%S")" | "$0}' /tmp/sse_raw.txt
echo ""
echo "=== 按 frame 解析（python） ==="
python3 << 'PYEOF'
from pathlib import Path
import json, re
raw = Path("/tmp/sse_raw.txt").read_bytes().decode("utf-8", errors="replace")
frames = raw.split("\n\n")
frames = [f for f in frames if f.strip()]
rc_count = 0
rc_total_chars = 0
reply_text = ""
reply_len = None
done_seen = False
start_seen = False
node_evts = []
err_evts = []
tool_evts = []
escalated_seen = False
debug_seen = False
for i, f in enumerate(frames):
    lines = f.split("\n")
    datas = []
    event_name = None
    for ln in lines:
        if not ln.strip():
            continue
        if ln.startswith("event:"):
            event_name = ln[len("event:"):].strip()
        elif ln.startswith("data:"):
            datas.append(ln[len("data:"):].lstrip())
        elif ln.startswith(":"):
            pass  # sse comment / flush hint
    if not datas:
        continue
    text = "\n".join(datas)
    try:
        obj = json.loads(text)
    except Exception as exc:
        print(f"[frame {i:02d}] !! non-JSON (len={len(text)}): {text[:160]!r}")
        continue
    t = obj.get("type") or event_name
    if t == "start":
        start_seen = True
        print(f"[frame {i:02d}] start  thread={obj.get('thread_id')!r}  user={obj.get('user_input')!r}")
    elif t == "reply_chunk":
        rc_count += 1
        tx = obj.get("text","") or ""
        rc_total_chars += len(tx)
        if rc_count <= 5 or rc_count % 30 == 0:
            print(f"[frame {i:02d}] reply_chunk #{rc_count}  len={len(tx)}  preview={tx[:40]!r}")
    elif t == "reply":
        reply_text = obj.get("text","") or ""
        reply_len = len(reply_text)
        print(f"[frame {i:02d}] reply  FINAL_LEN={reply_len}  text_preview={reply_text[:200]!r}")
    elif t == "done":
        done_seen = True
        print(f"[frame {i:02d}] done  extra_keys={list(obj.keys())}")
    elif t == "error":
        err_evts.append(obj)
        print(f"[frame {i:02d}] error  {obj}")
    elif t in ("node_start","node_end"):
        node_evts.append((t, obj.get("node","?"), len(obj.get("patch") or {})))
        patch_note = f"  patch_keys={list((obj.get('patch') or {}).keys())}" if t=="node_end" and obj.get("patch") else ""
        print(f"[frame {i:02d}] {t:10s} node={obj.get('node','?')}{patch_note}")
    elif t == "tool":
        tool_evts.append(obj)
        print(f"[frame {i:02d}] tool  kind={obj.get('kind')!r}")
    elif t == "escalated":
        escalated_seen = True
        print(f"[frame {i:02d}] escalated  ticket={obj.get('ticket_no')!r}")
    elif t == "debug":
        debug_seen = True
        print(f"[frame {i:02d}] debug  keys={list((obj.get('payload') or {}).keys()) if isinstance(obj.get('payload'),dict) else '?'}")
    else:
        print(f"[frame {i:02d}] type={t!r}  keys={list(obj.keys())}")
print("")
print("=" * 60)
print("SUMMARY:")
print(f"  start: {start_seen}   done: {done_seen}")
print(f"  node_events: {len(node_evts)}   tool_events: {len(tool_evts)}   error_events: {len(err_evts)}   escalated: {escalated_seen}   debug: {debug_seen}")
print(f"  reply_chunk_count = {rc_count}   reply_chunk_total_chars = {rc_total_chars}")
print(f"  reply.final_len = {reply_len}")
print("")
issues = []
if not start_seen: issues.append("NO start frame")
if not done_seen: issues.append("NO done frame (SSE interrupted or exception)")
if rc_count == 0: issues.append("NO STREAMING CHUNKS AT ALL (reply_chunk_count=0)")
elif rc_count < 3 and rc_total_chars > 100: issues.append(f"only {rc_count} reply_chunk for {rc_total_chars} chars -> chunks NOT streaming, all at once")
if reply_len is None: issues.append("NO reply final frame")
elif reply_len <= 12: issues.append(f"final reply is suspiciously short ({reply_len} chars): {reply_text!r}  (most likely 9-word fallback)")
if err_evts: issues.append(f"{len(err_evts)} SSE error events")
if len(node_evts) < 2: issues.append(f"only {len(node_evts)} node events; graph probably didn't even start/end")
print("ISSUES:")
for x in issues:
    print(f"  [!!] {x}")
PYEOF
