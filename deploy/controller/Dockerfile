# deploy/controller/Dockerfile
FROM python:3.12.9-slim-bookworm

RUN groupadd --gid 10001 bw-controller \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin bw-controller
COPY --chown=bw-controller:bw-controller . /opt/eastwatch
RUN python -m venv /opt/eastwatch/.venv \
    && /opt/eastwatch/.venv/bin/pip install --no-cache-dir '/opt/eastwatch[controller]'

ENV PATH="/opt/eastwatch/.venv/bin:/usr/local/bin:/usr/bin:/bin"
USER bw-controller
WORKDIR /var/lib/eastwatch
ENTRYPOINT ["bw-controller"]
