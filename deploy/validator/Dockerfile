# The validator image (docs/validators.md section 6): the validator, its interface, its health check and the updater
# that keeps them on the newest signed release (src/kokoah_subnet/validator/update.py). Built and signed by
# .github/workflows/release.yml on every change to main that reaches it; validators run it with compose.yml here.
# Every base image is pinned by digest.
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.19@sha256:b46b03ddfcfbf8f547af7e9eaefdf8a39c8cebcba7c98858d3162bd28cf536f6 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/kokoah-subnet/venv UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /src
# the pinned packages first (uv.lock), so a code change rebuilds only the last layer
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python3.12
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --python /usr/local/bin/python3.12

FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70
# the updater's tools: Docker's command line with its compose plugin, and cosign to check each release's signature
COPY --from=docker:29.9.2-cli@sha256:5e6baf67325a79f64ed3bed6863954d5e05d0f3d4e57cbdb14809346814bbfdb /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:29.9.2-cli@sha256:5e6baf67325a79f64ed3bed6863954d5e05d0f3d4e57cbdb14809346814bbfdb /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=ghcr.io/sigstore/cosign/cosign:v2.6.5@sha256:ad281047f85c5e1fc6ffbc30c2b55be3b07b4032bef715a12122ce5829619aca /ko-app/cosign /usr/local/bin/cosign
COPY --from=build /opt/kokoah-subnet/venv /opt/kokoah-subnet/venv
# the release's compose file: the updater installs it on the host with the release
COPY deploy/validator/compose.yml /opt/kokoah-subnet/compose.yml
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} +
ARG COMMIT=unknown
LABEL org.opencontainers.image.source="https://github.com/kokoahdotcom/kokoah-subnet" \
      org.opencontainers.image.revision="${COMMIT}" \
      org.opencontainers.image.title="Kokoah subnet validator"
ENV PATH=/opt/kokoah-subnet/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 KOKOAH_SUBNET_COMMIT=${COMMIT}
# compose.yml sets each service's user; nobody otherwise
USER 65534:65534
ENTRYPOINT ["kokoah-subnet"]
CMD ["--help"]
