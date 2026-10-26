FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY main.py ./

RUN useradd --uid 10001 --user-group --create-home appuser
USER 10001:10001

ENV PATH="/app/.venv/bin:$PATH" \
    BIND_HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

CMD ["python", "main.py"]