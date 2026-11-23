FROM docker.io/library/python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src/ src/
COPY skills/ skills/
COPY plugins/ plugins/
RUN python -m venv /opt/skillstore \
    && /opt/skillstore/bin/pip install --no-cache-dir PyYAML==6.0.3 .

FROM docker.io/library/python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
LABEL org.opencontainers.image.source="https://github.com/soulsteads/soulsteads-agent-skillstore"
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --home-dir /data/home --no-create-home app \
    && install -d -m 0700 -o 10001 -g 10001 /data /data/home
COPY LICENSE NOTICE /usr/share/doc/soulsteads-agent-skillstore/
COPY --from=build /opt/skillstore /opt/skillstore
ENV HOME=/data/home SKILLSTORE_STORE=/data/registry PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /data
STOPSIGNAL SIGINT
EXPOSE 8765
ENTRYPOINT ["/opt/skillstore/bin/skillstore"]
CMD ["--help"]
