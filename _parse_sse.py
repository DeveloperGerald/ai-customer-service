from pathlib import Path
import json
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
frame_count = len(frames)
first_rc_frame = None
last_rc_frame = None
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
            pass
    if not datas:
        continue
    text = "\n".join(datas)
    try:
        obj = json.loads(text)
    except Exception as exc:
        print(f"[frame {i:03d} of {frame_count}] !! non-JSON (len={len(text)}): {text[:120]!r}")
        continue
    t = obj.get("type") or event_name
    if t == "start":
        start_seen = True
        print(f"[frame {i:03d}/{frame_count}] START  thread={obj.get('thread_id')!r}  user={obj.get('user_input')!r}")
    elif t == "reply_chunk":
        rc_count += 1
        tx = obj.get("text","") or ""
        rc_total_chars += len(tx)
        if first_rc_frame is None:
            first_rc_frame = i
            print(f"[frame {i:03d}/{frame_count}] reply_chunk #{rc_count} FIRST  len={len(tx)}  preview={tx[:80]!r}")
        last_rc_frame = i
        if rc_count == 5 or rc_count == 15 or rc_count % 25 == 0:
            print(f"[frame {i:03d}/{frame_count}] reply_chunk #{rc_count}  len={len(tx)}  preview={tx[:80]!r}")
    elif t == "reply":
        reply_text = obj.get("text","") or ""
        reply_len = len(reply_text)
        print(f"[frame {i:03d}/{frame_count}] reply  FINAL_LEN={reply_len}  text[:300]={reply_text[:300]!r}")
    elif t == "done":
        done_seen = True
        print(f"[frame {i:03d}/{frame_count}] DONE  keys={list(obj.keys())}")
    elif t == "error":
        err_evts.append(obj)
        print(f"[frame {i:03d}/{frame_count}] ERROR  {obj}")
    elif t in ("node_start","node_end"):
        patch = obj.get("patch") or {}
        patch_keys = sorted(list(patch.keys()))[:10]
        node_evts.append((t, obj.get("node","?"), len(patch), patch_keys))
        patch_note = f"  patch_keys={patch_keys}" if t == "node_end" and patch else ""
        print(f"[frame {i:03d}/{frame_count}] {t:10s} node={obj.get('node','?')}{patch_note}")
    elif event_name == "node" and isinstance(obj, dict) and obj.get("phase") in ("start", "end"):
        # event: node + {phase: start|end, node: ...} is also the canonical wire format
        phase = obj.get("phase")
        t2 = f"node_{phase}"
        patch = obj.get("patch") or {}
        patch_keys = sorted(list(patch.keys()))[:10]
        node_evts.append((t2, obj.get("node","?"), len(patch), patch_keys))
        patch_note = f"  patch_keys={patch_keys}" if phase == "end" and patch else ""
        print(f"[frame {i:03d}/{frame_count}] {t2:10s} node={obj.get('node','?')}{patch_note}")
    elif t == "tool":
        tool_evts.append(obj)
        print(f"[frame {i:03d}/{frame_count}] tool  kind={obj.get('kind')!r}")
    elif t == "escalated":
        print(f"[frame {i:03d}/{frame_count}] escalated  ticket={obj.get('ticket_no')!r}")
    elif t == "debug":
        p = obj.get("payload")
        keys = list(p.keys()) if isinstance(p, dict) else "?"
        print(f"[frame {i:03d}/{frame_count}] debug  keys={keys}")
print("")
print("=" * 64)
print("SUMMARY:")
print(f"  total frames = {frame_count}")
print(f"  start = {start_seen}, done = {done_seen}")
print(f"  node_events = {len(node_evts)}, tool_events = {len(tool_evts)}, error_events = {len(err_evts)}")
print(f"  reply_chunk_count = {rc_count}   reply_chunk_total_chars = {rc_total_chars}")
print(f"  FIRST reply_chunk at frame #{first_rc_frame}   LAST at frame #{last_rc_frame}")
print(f"  final reply.len = {reply_len}")
if last_rc_frame and first_rc_frame and last_rc_frame > first_rc_frame:
    spread = last_rc_frame - first_rc_frame + 1
    print(f"  reply_chunk SPAN across frames {first_rc_frame}..{last_rc_frame} = {spread} frames")
else:
    print("  !! reply_chunk NOT spread across frames (BATCHED AT END)")
print("")
issues = []
if not start_seen: issues.append("NO start frame")
if not done_seen: issues.append("NO done frame (SSE interrupted or exception)")
if rc_count == 0:
    issues.append("NO STREAMING CHUNKS AT ALL (reply_chunk_count=0)")
elif last_rc_frame and (frame_count - last_rc_frame) > rc_count:
    issues.append(f"reply_chunks LATE: first={first_rc_frame}, last={last_rc_frame}, total_frames={frame_count}")
if reply_len is None:
    issues.append("NO reply final frame")
elif reply_len <= 12:
    issues.append(f"final reply IS 9-WORD FALLBACK! len={reply_len}: {reply_text!r}")
if err_evts:
    issues.append(f"{len(err_evts)} SSE error events: {err_evts}")
if len(node_evts) < 2:
    issues.append(f"only {len(node_evts)} node events; graph probably didn't even start/end")
print("ISSUES:")
for x in issues:
    print(f"  [!!] {x}")
