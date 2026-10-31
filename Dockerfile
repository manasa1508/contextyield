FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never UV_PYTHON=/usr/local/bin/python3.12 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY contextyield ./contextyield
COPY migrations ./migrations
COPY config ./config
COPY fixtures ./fixtures
COPY scripts ./scripts
RUN chmod -R a+rX /app && pip install --no-cache-dir uv==0.12.7 && \
    uv sync --frozen --no-dev --no-editable && chmod -R a+rX /app && \
    groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "contextyield.api:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
