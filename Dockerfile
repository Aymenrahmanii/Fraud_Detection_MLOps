# ---------- Base Image ----------
FROM python:3.12-slim

# Set working directory inside container
WORKDIR /app

# ---------- Install dependencies ----------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------- Copy application code ----------
# API/mlruns holds the MLflow local registry used by services.py's
# tracking URI (file:./API/mlruns); it's included here since it lives
# under API/.
COPY API ./API

# ---------- Run as non-root ----------
RUN useradd --create-home --shell /bin/false appuser \
    && chown -R appuser:appuser /app
USER appuser

# Expose FastAPI port
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health').read()" || exit 1

# ---------- Run FastAPI ----------
CMD ["uvicorn", "API.main:app", "--host", "0.0.0.0", "--port", "8000"]
