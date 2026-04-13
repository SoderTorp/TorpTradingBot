FROM python:3.12-slim

RUN apt-get update && apt-get install -y cron && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

COPY crontab /etc/cron.d/tradingbot
RUN chmod 0644 /etc/cron.d/tradingbot && crontab /etc/cron.d/tradingbot

RUN mkdir -p /app/logs /app/state

RUN chmod +x entrypoint.sh

CMD ["./entrypoint.sh"]
