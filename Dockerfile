FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=ICT-7

WORKDIR /app

# Install dependency dulu supaya layer ini ter-cache dan build berikutnya cepat.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Baru salin kode. .dockerignore memastikan .env TIDAK ikut masuk image.
COPY core/ ./core/
COPY brand/ ./brand/
COPY config/ ./config/
COPY main.py .

RUN mkdir -p /app/logs

# Pilih --mode untuk menjalankan semua brand aktif di config/brands.yaml.
# Tambahkan --brand bila hanya ingin menjalankan satu brand.
ENTRYPOINT ["python", "main.py"]
