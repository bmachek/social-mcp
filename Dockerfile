FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN useradd --create-home --uid 10001 app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY server.py meta_client.py local_files.py exif_reader.py scheduler.py autopilot.py ./

USER app

# MCP servers speak JSON-RPC over stdio. Keep stdout clean — logs go to stderr.
CMD ["python", "-u", "server.py"]
