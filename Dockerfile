FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /srv

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --upgrade pip && pip install .

COPY frontend ./frontend
COPY data ./data

# Run as non-root.
RUN useradd --create-home buddy && mkdir -p /srv/.data && chown -R buddy /srv
USER buddy

EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.fast_api_app:app --host 0.0.0.0 --port ${PORT}"]
