# ── Bridge (Node.js + Baileys) ────────────────────────────────────
FROM node:20-alpine AS bridge

WORKDIR /app
COPY bridge-baileys/package.json bridge-baileys/package-lock.json ./
RUN npm ci --production
COPY bridge-baileys/index.js .

EXPOSE 3080
CMD ["node", "index.js"]

# ── Backend (Python + FastAPI) ───────────────────────────────────
FROM python:3.11-slim AS backend

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN touch README.md && uv sync --frozen --no-dev

COPY src/ src/
COPY static/ static/

EXPOSE 8080
CMD ["uv", "run", "uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8080"]
