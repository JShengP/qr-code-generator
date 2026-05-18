# Single-stage image. The build is small enough that the layer-cache
# benefit of multi-stage doesn't outweigh the extra Dockerfile
# complexity. Switch to multi-stage if we ever add a compile step
# (Cython, native deps, etc.).
FROM python:3.12-slim

# Don't write .pyc files; flush stdout (so Render's log stream is
# realtime instead of buffered through Python).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# requirements.txt first for layer caching — `pip install` only re-runs
# when deps change, not when app code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Then app code + static. .dockerignore (alongside this file) keeps
# .venv, tests/, .git out of the image.
COPY app/ ./app/
COPY static/ ./static/

# Render sets $PORT dynamically; default to 8000 for local
# `docker run -p 8000:8000`. The shell-form CMD is intentional —
# exec-form `["uvicorn", ...]` doesn't expand $PORT.
ENV PORT=8000
EXPOSE 8000

CMD uvicorn app.main:app --host 0.0.0.0 --port $PORT
