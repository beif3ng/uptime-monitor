# AI assistance: parts of this file were written with Claude (Anthropic).
# Architecture, review and debugging are my own. See README.md, "AI use".

# The same Python as the development machine, so a version difference can
# never explain "it works locally".
FROM python:3.14-slim

WORKDIR /app

# Dependencies first: they change far less often than the code, so rebuilds
# after an edit reuse this layer instead of downloading everything again.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The database lives outside the image, on the volume mounted by compose,
# so the history survives a rebuild.
ENV MONITOR_DB=/data/monitor.db

EXPOSE 8000

# Exactly one worker. Every worker would run its own scheduler, and every
# scheduler would check every monitor and send its own alerts.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
