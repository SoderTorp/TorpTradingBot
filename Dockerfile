FROM python:3.12-slim

RUN apt-get update && apt-get install -y cron && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

COPY scheduler/cron_jobs.sh /tmp/crontab
RUN crontab /tmp/crontab && rm /tmp/crontab

RUN mkdir -p /app/logs /app/state

RUN chmod +x entrypoint.sh

CMD ["./entrypoint.sh"]
