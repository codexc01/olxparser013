FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DB_PATH=/data/olx.sqlite3
WORKDIR /srv/bot
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -m -u 10001 monitor && install -d -o monitor /data
COPY . .
USER monitor
VOLUME ["/data"]
CMD ["python", "run.py"]
