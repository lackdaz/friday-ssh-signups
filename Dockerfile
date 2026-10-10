FROM python:3.12-slim

# tzdata as a pip package so zoneinfo always finds Asia/Singapore,
# whatever the base image ships with.
RUN pip install --no-cache-dir "asyncssh>=2.14,<3" tzdata

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=2222

WORKDIR /app
COPY server.py .

# Runs as whatever user compose sets (defaults to 1000:1000), so files in
# the bind-mounted ./data stay owned by you on the host.
EXPOSE 2222
CMD ["python", "server.py"]