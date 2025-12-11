FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

# system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# non-root user for security
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

ENV PATH="/home/appuser/.local/bin:${PATH}"
CMD ["python","bot.py"]
