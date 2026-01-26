FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create data directory for SQLite
RUN mkdir -p /app/data /app/logs

# Run the smart money scanner v3.1 (memory-safe + ROI optimized)
# Interval 300s (5 min) prevents API blocking, rate limiter adds extra protection
CMD ["python", "-m", "cli.main", "scan", "--interval", "300", "--min-bet", "2000", "--telegram", "--copy-size", "10"]
