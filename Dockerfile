FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create data directory for SQLite
RUN mkdir -p /app/data /app/logs

# Run the smart money scanner with stricter insider detection
CMD ["python", "-m", "cli.main", "scan", "--interval", "60", "--min-bet", "2000", "--telegram", "--copy-size", "10"]
