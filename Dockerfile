# Use an official minimal Python image
FROM python:3.11-slim

# Prevent Python from writing .pyc files and set stdout/stderr unbuffered
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=off

WORKDIR /app

# Install system dependencies needed for some Python packages (aiohttp, cryptography, etc.)
# Keep packages minimal to reduce image size.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       libssl-dev \
       libffi-dev \
       wget \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies first (cache layer)
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip setuptools wheel \
    && pip install -r /app/requirements.txt

# Copy application code
COPY . /app

# Create a non-root user and switch to it
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

# Expose the port Flask listens on (Render will set PORT env)
EXPOSE 3000

# Recommended environment variables (set on Render)
# BOT_TOKEN, OWNER_USER_IDS, STATUS_PAGE_URL, etc.

# Command to run the bot; Render will run this inside the container.
CMD ["python", "bot.py"]
