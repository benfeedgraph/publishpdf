FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
COPY . .
RUN useradd --create-home app && chown -R app /app
USER app
# Default: the background worker (applies pending migrations first). The API on Vercel
# never runs migrations, so this is what keeps the database schema current.
# A host running the API from this image overrides the command (see railway.json).
CMD ["sh", "-c", "python -m app.cli migrate && exec python -m app.cli worker"]
