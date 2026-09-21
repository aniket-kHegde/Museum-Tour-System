FROM python:3.11-slim

WORKDIR /app

# Install system deps for sentence-transformers and paho
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ curl \
    && rm -rf /var/lib/apt/lists/*

# Copy shared utilities (build context is ./services so paths are qualified)
COPY shared /app/shared

# Install Python dependencies
COPY qa_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download embedding model so container starts instantly
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Copy service code
COPY qa_service/main.py .

CMD ["python", "main.py"]
