FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir \
    paho-mqtt \
    requests

# Copy entire project (config, data, llm, simulation, etc.)
COPY . .

# Set working directory to simulation for default command
WORKDIR /app/simulation

# Default command
CMD ["python", "simulate.py", "normal"]
