"""把仓库根 .env 的 LANGSMITH__* 行同步到 backend/.env（去重，优先覆盖）。"""
from pathlib import Path

root_env_path = Path("/Users/w7v2i9aa2/Workspace/ai-customer-service/.env")
backend_env_path = Path("/Users/w7v2i9aa2/Workspace/ai-customer-service/backend/.env")

if not backend_env_path.exists():
    example = backend_env_path.with_name(".env.example")
    backend_env_path.write_text(example.read_text() if example.exists() else "")

root_lines = root_env_path.read_text().splitlines()
langsmith_lines = [ln for ln in root_lines if ln.strip().startswith("LANGSMITH__")]

backend_lines = backend_env_path.read_text().splitlines()
# 去掉 backend 里已有的 LANGSMITH__ 行（避免重复），再追加
filtered = [ln for ln in backend_lines if not ln.strip().startswith("LANGSMITH__")]
if filtered and filtered[-1].strip() != "":
    filtered.append("")
filtered.append(
    "# -------- LangSmith 可观测（与仓库根 .env 同步写入，保证 cd backend && uvicorn 也能读到）--------"
)
filtered.extend(langsmith_lines)
backend_env_path.write_text("\n".join(filtered) + "\n")

print(f"Root .env LANGSMITH__ lines = {len(langsmith_lines)}")
for ln in langsmith_lines:
    if "API_KEY" in ln and "=" in ln:
        k, v = ln.split("=", 1)
        masked = "*" * 8 + v[-6:] if len(v) > 14 else "(short value)"
        print(f"  {k}= {masked}")
    else:
        print(" ", ln)
print("backend/.env 写入完成，行数 =", len(backend_env_path.read_text().splitlines()))
