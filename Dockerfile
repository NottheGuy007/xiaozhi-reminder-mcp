FROM python:3.11-slim

# Install build deps for psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev build-essential curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
ENV XIAOZHI_WS_URL=wss://xiaozhi.example/ws

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--loop", "asyncio"]
