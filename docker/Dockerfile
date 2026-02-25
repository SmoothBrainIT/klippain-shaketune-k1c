FROM python:3.11-slim

WORKDIR /app

# Install system dependencies needed by matplotlib/numpy
RUN apt-get update && apt-get install -y --no-install-recommends \
    libopenblas-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy and install server-specific Python dependencies first (better layer caching)
COPY docker/requirements_server.txt /app/docker/requirements_server.txt
RUN pip install --no-cache-dir -r /app/docker/requirements_server.txt

# Copy the full shaketune package (the server uses it directly for graph generation)
COPY shaketune/ /app/shaketune/
COPY docker/server.py /app/docker/server.py

# Klipper's shaper_calibrate module is needed for input shaper graph generation.
# We bundle a compatible copy here so the server is self-contained.
# Users can also mount their own Klipper directory via the KLIPPER_DIR env var.
ENV SHAKETUNE_IN_CLI=1
ENV KLIPPER_DIR=/app/klipper

# Optional: mount a Klipper source tree at /app/klipper for shaper_calibrate access.
# The server will attempt to load it if KLIPPER_DIR is set and the directory exists.

EXPOSE 8080

CMD ["uvicorn", "docker.server:app", "--host", "0.0.0.0", "--port", "8080"]
