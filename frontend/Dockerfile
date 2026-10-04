# -------- 手串售后智能客服前端 --------
# 开发模式（Vite dev server + 自动 /api 反代到 backend:8000）
FROM node:20-alpine AS frontend-dev

WORKDIR /app
ENV HOST=0.0.0.0
ENV PORT=5174

EXPOSE 5174

CMD ["sh", "-c", "npm install --no-audit --no-fund && npm run dev -- --host 0.0.0.0 --port 5174"]

# -------- 生产构建（可选：nginx 托管 dist/）--------
FROM node:20-alpine AS builder
WORKDIR /src
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine AS frontend-prod
COPY --from=builder /src/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
