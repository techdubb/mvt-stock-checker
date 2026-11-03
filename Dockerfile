FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATE_FILE=/data/state.json

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
RUN useradd --system --uid 10001 watcher && mkdir /data && chown watcher /data
USER watcher
VOLUME ["/data"]

CMD ["python", "watcher.py"]
