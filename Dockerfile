FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv/bot
COPY requirements-legacy.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && useradd -m -u 10001 monitor
COPY . .
USER monitor
CMD ["sh","-ec","alembic upgrade head && exec python -m app.main"]
