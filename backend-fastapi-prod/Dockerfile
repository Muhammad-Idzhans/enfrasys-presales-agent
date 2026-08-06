# Lightweight Python 3.11 Linux image
FROM python:3.11-slim

# Prevent .pyc files and buffer issues
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . /app

# Expose the FastAPI port
EXPOSE 8000

# Start FastAPI with Uvicorn (file is app_test.py → app_test:app)
CMD ["uvicorn", "app_test:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]