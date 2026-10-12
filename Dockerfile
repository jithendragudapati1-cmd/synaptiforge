FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SYNAPTIFORGE_ENV=production SYNAPTIFORGE_STORAGE_DIR=/var/lib/synaptiforge COOKIE_SECURE=true OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 synaptiforge && mkdir -p /var/lib/synaptiforge && chown synaptiforge:synaptiforge /var/lib/synaptiforge
COPY --chown=synaptiforge:synaptiforge *.py ./
COPY --chown=synaptiforge:synaptiforge templates ./templates
COPY --chown=synaptiforge:synaptiforge static ./static
USER synaptiforge
EXPOSE 8080
CMD ["python","run_production.py"]
