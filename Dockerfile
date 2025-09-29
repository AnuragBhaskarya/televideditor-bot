# Start with a slim, official Python base image
FROM python:3.10-slim

# Set a working directory inside the container
WORKDIR /app

# Update the package manager and install ffmpeg.
# The `-y` flag auto-confirms the installation.
# `&& rm -rf /var/lib/apt/lists/*` cleans up to keep the image size small.
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Copy the requirements file first to leverage Docker's layer caching.
# This way, dependencies are only re-installed if requirements.txt changes.
COPY requirements.txt .

# Install the Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code (e.g., televideditor.py)
COPY . .

# Railway provides the PORT environment variable. We'll tell Gunicorn to bind to it.
# The default is 8080 if the variable isn't set. We use 1 worker because
# the bot's lock already serializes the heavy processing tasks.
CMD ["gunicorn", "--bind", "0.0.0.0:${PORT:-8080}", "--workers", "1", "televideditor:app"]
