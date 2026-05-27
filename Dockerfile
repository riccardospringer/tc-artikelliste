FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TC_HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

COPY src/ ./src/
COPY fixtures/ ./fixtures/

EXPOSE 8080

CMD ["python3", "src/server.py"]
