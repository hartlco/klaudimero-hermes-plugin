FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
LABEL org.opencontainers.image.title="Klaudimero push relay" \
      org.opencontainers.image.description="Encrypted notification relay for Klaudimero and Hermes" \
      org.opencontainers.image.source="https://github.com/hartlco/klaudimero-hermes-plugin" \
      org.opencontainers.image.url="https://github.com/hartlco/klaudimero-hermes-plugin/blob/main/docs/relay.md" \
      org.opencontainers.image.version="0.3.0"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY relay/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY relay/__init__.py relay/app.py relay/managed.py relay/body_limit.py /app/relay/
COPY klaudimero_plugin/__init__.py klaudimero_plugin/relay_auth.py /app/klaudimero_plugin/
RUN useradd --uid 10001 --create-home relay && mkdir /data && chown relay:relay /data && chmod 700 /data
USER relay
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import http.client; c=http.client.HTTPConnection('127.0.0.1',8080,timeout=3); c.request('GET','/v1/status'); raise SystemExit(0 if c.getresponse().status == 401 else 1)"]
CMD ["uvicorn", "relay.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
