FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create data directory for SQLite
RUN mkdir -p /app/data /app/logs

# Run the bot with arbitrage strategy
CMD ["python", "-m", "cli.main", "run", "--strategy", "momentum", "--interval", "60"]
