FROM node:22-bookworm-slim AS node_runtime

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN node --version && ffmpeg -version | head -n 1
COPY main.py .
COPY banner.png .
RUN python -m py_compile main.py && useradd --create-home --uid 10001 botuser
USER botuser
CMD ["python", "main.py"]
