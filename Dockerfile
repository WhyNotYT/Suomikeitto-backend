# Use official Python base image

FROM python:3.12-slim

# Set environment variables

ENV PYTHONDONTWRITEBYTECODE=1 
ENV PYTHONUNBUFFERED=1 
ENV PORT=5000 
ENV HOST=0.0.0.0

# Create a non-root user (OpenShift requires non-root)

RUN useradd -m gameuser

# Set working directory

WORKDIR /app

# Copy requirements (if you have a separate requirements.txt)

# For this script, aiohttp is the only dependency

RUN pip install --no-cache-dir aiohttp

# Copy application code

COPY . /app

# Set ownership for non-root user

RUN chown -R gameuser:gameuser /app

# Switch to non-root user

USER gameuser

# Expose port

EXPOSE 5000

# Set entrypoint

ENTRYPOINT ["python", "app.py"]
