FROM python:3.11-slim

# Install system libraries required by OpenCV & YOLO
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    build-essential \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirement file from root build context
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Edge source files
COPY shared/ /app/shared/
COPY ingestion/ /app/ingestion/
COPY edge/ /app/edge/
COPY edge/ /app/
COPY cameras.yaml config.yaml /app/

ENV PYTHONPATH=/app

CMD ["python", "main.py"]
