FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data
ENV EMS_DATA_DIR=/data PORT=5000
EXPOSE 5000
CMD ["sh","-c","gunicorn --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:${PORT:-5000} app_v11:app"]
