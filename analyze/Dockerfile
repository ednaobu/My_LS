FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.27" "httpx>=0.27" "pydantic>=2.5" pypdf
COPY analyze_app/ analyze_app/
COPY samples/ samples/
ENV ANALYZE_DB=/data/analyze.db
EXPOSE 9000
CMD ["uvicorn", "analyze_app.main:app", "--host", "0.0.0.0", "--port", "9000"]
