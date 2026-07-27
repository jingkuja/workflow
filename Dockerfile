FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system workflow \
    && useradd --system --gid workflow --home-dir /app workflow \
    && mkdir -p /data/probes /data/files \
    && chown -R workflow:workflow /app /data/probes /data/files

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations

RUN --mount=type=cache,target=/root/.cache/pip pip install .

USER workflow

CMD ["uvicorn", "workflow.apps.workflow_api:app", "--host", "0.0.0.0", "--port", "8000"]
