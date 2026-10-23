FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir 'fastapi>=0.115,<1' 'uvicorn>=0.34,<1' 'httpx[http2]>=0.28,<1' 'PyJWT[crypto]>=2.15.1,<3'
COPY relay/__init__.py relay/app.py /app/relay/
RUN useradd --uid 10001 --create-home relay && mkdir /data && chown relay:relay /data
USER relay
EXPOSE 8080
CMD ["uvicorn", "relay.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
