FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "-m", "swarm_orchestrator.api"]
