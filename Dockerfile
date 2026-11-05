FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    PORT=8080

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install . \
    && useradd --uid 10001 --create-home app \
    && mkdir -p /data && chown app:app /data

USER app
VOLUME ["/data"]
EXPOSE 8080
CMD ["python", "-m", "multi_google_mcp"]
