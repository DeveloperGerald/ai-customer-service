FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# ---- Debian 12 (bookworm) 国内镜像兜底：清华 Tuna ----
# 若本机/CI 在海外，apt 直连 deb.debian.org 就好；这一步失败会自动回退默认源
RUN set -eux; \
    if command -v apt-get >/dev/null 2>&1; then \
      cp /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list.d/debian.sources.bak || true; \
      if grep -q '^Types:' /etc/apt/sources.list.d/debian.sources 2>/dev/null; then \
        sed -i 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g; s|http://security.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list.d/debian.sources || true; \
      fi; \
      apt-get update -o Acquire::Retries=3 --fix-missing || apt-get update -o Acquire::Retries=3; \
      apt-get install -y --no-install-recommends --fix-missing \
        build-essential libpq-dev curl ca-certificates \
      || apt-get install -y --no-install-recommends \
        build-essential libpq-dev curl ca-certificates; \
      rm -rf /var/lib/apt/lists/*; \
    fi

COPY pyproject.toml ./
# 真接入：装 demo + llm + vector 全量 extras
#   demo   → orjson / python-json-logger / python-multipart
#   llm    → openai / langgraph / tenacity 等真实大模型调用
#   vector → pgvector SQL 原生 <=> 算子（可切 Python 侧余弦，装了不坏事）
RUN set -eux; \
    pip install --upgrade pip --index-url https://pypi.tuna.tsinghua.edu.cn/simple || \
    pip install --upgrade pip; \
    pip install --no-cache-dir hatchling \
      --index-url https://pypi.tuna.tsinghua.edu.cn/simple || \
    pip install --no-cache-dir hatchling; \
    pip install --no-cache-dir ".[demo,llm,vector]" \
      --index-url https://pypi.tuna.tsinghua.edu.cn/simple || \
    pip install --no-cache-dir ".[demo,llm]" \
      --index-url https://pypi.tuna.tsinghua.edu.cn/simple || \
    pip install --no-cache-dir ".[demo,llm,vector]" || \
    pip install --no-cache-dir ".[demo,llm]" || \
    pip install --no-cache-dir "."

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
