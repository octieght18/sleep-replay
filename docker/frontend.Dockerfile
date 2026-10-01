FROM public.ecr.aws/docker/library/python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db
WORKDIR /app
COPY backend backend
COPY frontend frontend
ENV SLEEP_REPLAY_FRONTEND_HOST=0.0.0.0 SLEEP_REPLAY_BACKEND_URL=http://backend:8735 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8734
CMD ["python", "-m", "backend.api.static_server"]
