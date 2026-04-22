# Optional: containerized deployment. Native systemd is the canonical path.
FROM python:3.12-slim-bookworm AS builder
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir build && python -m build --wheel

FROM python:3.12-slim-bookworm
RUN useradd --system --no-create-home --shell /usr/sbin/nologin alfredcoo
WORKDIR /app
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
USER alfredcoo
ENV LOG_FORMAT=json
EXPOSE 8090
ENTRYPOINT ["python", "-m", "alfred_coo"]
