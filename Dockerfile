FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data && chmod 777 /app/data

# PORT é injetado pelo Railway em runtime — usa shell form para expandir a variável
CMD uvicorn src.webhook:app --host 0.0.0.0 --port ${PORT:-8000}
