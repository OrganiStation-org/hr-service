# Stage 1: Build/Deps
FROM python:3.11-slim AS build
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-slim
WORKDIR /app
RUN useradd --create-home appuser
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
COPY --from=build /usr/local /usr/local
COPY --chown=appuser:appuser app.py .
USER appuser
EXPOSE 8002
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8002"]
