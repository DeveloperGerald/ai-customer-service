"""验证 LangSmith 配置是否生效（避免 heredoc 被 shell 吃掉引号）。"""
import os
import structlog
from app.config import load_settings
from app.main import _configure_langsmith

s = load_settings()
_key = s.langsmith.api_key
_key_ok = (_key is not None) and (len(_key.get_secret_value()) > 0)

print("[1] CWD              =", os.getcwd())
print("[2] tracing_enabled  =", s.langsmith.tracing_enabled, "| type:", type(s.langsmith.tracing_enabled).__name__)
print("[3] project          =", s.langsmith.project)
print("[4] endpoint         =", s.langsmith.endpoint)
print(
    "[5] api_key loaded?  =",
    f"YES (len={len(_key.get_secret_value())}, last-6={_key.get_secret_value()[-6:]})"
    if _key_ok
    else "NO",
)
print("[6] enabled (AND)    =", s.langsmith.tracing_enabled and _key_ok)
print(
    "[7] main._configure 判断结果 =",
    "ENABLED" if (s.langsmith.tracing_enabled and _key_ok) else "DISABLED",
)
print()
print("--- 调用 main._configure_langsmith 后 os.environ 实际写入 ---")
log = structlog.get_logger("verify")
_configure_langsmith(s, log)
print()
for k in (
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_API_KEY",
    "LANGCHAIN_ENDPOINT",
    "LANGCHAIN_PROJECT",
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
):
    v = os.environ.get(k)
    if k.endswith("API_KEY") and v:
        v = f"len={len(v)} last-6={v[-6:]}"
    print(f"  os.environ[{k:30s}] = {v!r}")
