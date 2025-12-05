# Use official Python base image
FROM python:3.12-slim



# Environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=5000
ENV HOST=0.0.0.0

RUN apt-get update && \
    apt-get install -y --no-install-recommends bash procps nano && \
    rm -rf /var/lib/apt/lists/*
# Create non-root user for OpenShift
RUN useradd -m gameuser


# Working directory
WORKDIR /app

# Install dependencies
# Adds watchfiles for auto-reload; remove if undesired
RUN pip install --no-cache-dir aiohttp watchfiles

# Copy application code
COPY . /app

# Set permissions
RUN chown -R gameuser:gameuser /app

# Switch to non-root user
USER gameuser

# Expose port
EXPOSE 5000

# Default command (can be overridden)
# Use a small entry script so you can override the command easily
CMD ["python", "app.py"]
