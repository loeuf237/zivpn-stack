# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS native-build
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY config/artifacts.json config/artifacts.json
COPY tools/build.py tools/download.py tools/
COPY native/ native/
# The build script verifies the Go archive against config/artifacts.json.
RUN python3 tools/build.py

FROM ghcr.io/astral-sh/uv:0.13.1 AS uv
FROM python:3.12-slim-bookworm AS python-build
COPY --from=uv /uv /bin/uv
WORKDIR /opt/zivpn
ENV UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

FROM python:3.12-slim-bookworm AS runtime
LABEL org.opencontainers.image.source="https://github.com/loeuf237/zivpn-stack" \
      org.opencontainers.image.description="ZiVPN native UDP server with SQLite account authentication"
ENV PATH="/opt/zivpn/.venv/bin:$PATH" PYTHONPATH=/usr/local/lib \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=python-build /opt/zivpn/.venv /opt/zivpn/.venv
COPY --from=native-build /build/build/zivpn-native /usr/local/bin/zivpn-native
COPY src/zivpn_*.py /usr/local/lib/
COPY --chmod=755 scripts/zivpn-native-auth.py /usr/local/sbin/zivpn-native-auth
COPY --chmod=755 scripts/zivpn-native-save-traffic /usr/local/sbin/zivpn-native-save-traffic
COPY --chmod=755 deploy/docker/entrypoint.sh /usr/local/bin/docker-entrypoint
COPY deploy/docker/healthcheck.py /usr/local/lib/zivpn_container_healthcheck.py
EXPOSE 5667/udp 5668/udp
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python3", "/usr/local/lib/zivpn_container_healthcheck.py"]
ENTRYPOINT ["/usr/local/bin/docker-entrypoint"]
CMD ["/usr/local/bin/zivpn-native", "-config", "/etc/zivpn/config.json", "-secondary", ":5668"]
