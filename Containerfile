FROM python:3.14-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app app
COPY schemas schemas
COPY policy policy
COPY tools tools

RUN useradd --create-home dwight && chown -R dwight:dwight /app
USER dwight

EXPOSE 8000

# --no-sync: the venv was built above, as root. Without this, `uv run` tries to
# re-sync at container start as the non-root user and fails on /app/.venv.
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
