# One image for every service (worker, review API, Streamlit UI, migrations): they share the code and differ only in the command.
# Build:  docker compose build        Run:  docker compose up -d        (see infra/docker/README.md)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# A fixed, unprivileged user: nothing in this system needs root.
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
COPY ui/requirements.txt /tmp/ui-requirements.txt
RUN pip install -r /tmp/requirements.txt -r /tmp/ui-requirements.txt

# .dockerignore keeps .env, secrets/, .git and the tests out of the image: credentials are supplied at run time, never baked in.
COPY --chown=app:app . /app
USER app

CMD ["python", "-m", "orchestrator.worker_main", "run"]
