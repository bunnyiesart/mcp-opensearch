FROM python:3.12.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY lib/ lib/

RUN adduser --disabled-password --no-create-home appuser
USER appuser

ENTRYPOINT ["python3", "server.py"]
