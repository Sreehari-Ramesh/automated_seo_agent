FROM python:3.11.9-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (layer-cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir "numpy==1.26.4" "pandas==2.2.3"

# Copy source — .dockerignore excludes .env, __pycache__, .git, data/
COPY . .

# Install package in editable mode
RUN pip install --no-cache-dir -e .

# Create app user, home dir, and writable directories for Streamlit
RUN useradd -r -u 1001 -m -d /home/appuser -s /sbin/nologin appuser && \
    mkdir -p /app/data /home/appuser/.streamlit && \
    chown -R appuser:appuser /app /home/appuser

ENV HOME=/home/appuser

USER appuser

CMD ["streamlit", "run", "streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8500"]