FROM python:3.11-slim

# Install libpq-dev for PostgreSQL adapter compilation and build utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirement file from root build context
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Central API source files
COPY shared/ /app/shared/
COPY ingestion/ /app/ingestion/
COPY central/ /app/central/
COPY central/ /app/
COPY cameras.yaml config.yaml /app/

ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
