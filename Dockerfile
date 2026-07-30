# One image for both services (worker + api); compose picks the command.
FROM python:3.12-slim
WORKDIR /app
COPY requirements-service.txt .
RUN pip install --no-cache-dir -r requirements-service.txt
COPY . .
# Unbuffered so `docker compose logs` streams the worker's progress lines;
# no bytecode so dev bind mounts don't litter the host tree with __pycache__.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
