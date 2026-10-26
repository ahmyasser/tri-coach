FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY tricoach ./tricoach
RUN pip install .

RUN useradd --create-home --uid 1000 coach && mkdir -p /data && chown coach /data
USER coach
VOLUME ["/data"]
EXPOSE 8000
CMD ["tricoach", "serve"]
