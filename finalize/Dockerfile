# One image, many services: SERVICE picks which one this container runs.
# (Teams often use one image per service instead, so each can pick its own
# dependencies and release cadence.)
FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.27" "httpx>=0.27" "pydantic>=2.5"
COPY shared/ shared/
COPY services/ services/
ENV PYTHONPATH=/app/shared DATA_DIR=/data
ARG SERVICE
ENV SERVICE=${SERVICE}
EXPOSE 8000
CMD uvicorn main:app --app-dir services/${SERVICE} --host 0.0.0.0 --port 8000
