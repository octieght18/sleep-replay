FROM public.ecr.aws/docker/library/python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db
WORKDIR /app
COPY requirements.txt .
# Optional build-only CA for environments using a TLS inspection proxy.
RUN --mount=type=secret,id=proxy_ca,required=false \
    if [ -f /run/secrets/proxy_ca ]; then \
      PIP_CERT=/run/secrets/proxy_ca python -m pip install --no-cache-dir -r requirements.txt; \
    else python -m pip install --no-cache-dir -r requirements.txt; fi
COPY backend backend
COPY tools tools
COPY sample_data sample_data
ENV SLEEP_REPLAY_DATA_DIR=/data SLEEP_REPLAY_API_HOST=0.0.0.0 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8735
CMD ["python", "-m", "backend.api.app"]
