FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt pyproject.toml MANIFEST.in ./
COPY agent6_mlops_lifecycle ./agent6_mlops_lifecycle
RUN pip install --no-cache-dir -r requirements.txt .
COPY API ./API
COPY Src/*.py ./Src/
COPY Pages ./Pages
COPY Images ./Images
COPY app.py ./
COPY Notebooks/artifacts ./Notebooks/artifacts
RUN useradd --create-home --shell /bin/false appuser && mkdir -p /app/artifacts && chown -R appuser:appuser /app
USER appuser
EXPOSE 8026
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8026/health').read()" || exit 1
CMD ["python", "-m", "agent6_mlops_lifecycle", "serve", "--host", "0.0.0.0", "--port", "8026"]
