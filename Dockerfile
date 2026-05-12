FROM python:3.12-slim

WORKDIR /app

# Install deps first for better Docker layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY prompts/ ./prompts/

# .env is mounted at runtime via docker-compose, not baked in.
CMD ["python", "-u", "bot.py"]
