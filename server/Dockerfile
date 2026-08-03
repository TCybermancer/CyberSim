# CyberSim orchestrator -- see docs/README.md for the OOB network model
# this is meant to run inside.
FROM python:3.11-slim

WORKDIR /app

# Install deps first so this layer caches across code-only rebuilds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The SQLite DB lives under /data so it survives container recreation
# via a mounted volume (see docker-compose.yml); CYBERSIM_DB_PATH is read
# by db.py. Bind-mount a host directory over /app/scenarios instead of
# rebuilding the image to change available scenarios.
ENV CYBERSIM_DB_PATH=/data/cybersim.db
RUN mkdir -p /data

RUN useradd --create-home --shell /usr/sbin/nologin cybersim \
    && chown -R cybersim:cybersim /app /data
USER cybersim

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
