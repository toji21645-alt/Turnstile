FROM python:3.11-slim-bookworm

WORKDIR /app

# Install system dependencies for Playwright on Debian Bookworm (stable)
RUN apt-get update && apt-get install -y \
    wget \
    curl \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright with dependencies using the official method
RUN pip install --no-cache-dir playwright==1.42.0 && \
    playwright install chromium && \
    playwright install-deps chromium

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY turnstile_solver.py .

# Set environment variables
ENV HEADLESS=true
ENV BROWSER_TYPE=chromium
ENV THREAD_COUNT=3
ENV DEBUG=false
ENV PYTHONUNBUFFERED=1

# Expose port
EXPOSE ${PORT:-5000}

# Run with hypercorn for better performance
CMD ["hypercorn", "turnstile_solver:app", "--bind", "0.0.0.0:${PORT:-5000}"]