# deploy/headroom/Dockerfile — Headroom proxy for hosted GPT jobs.
# Serves an OpenAI-compatible API on 8787 inside the bw networks; exchanges
# the workspace owner's Copilot OAuth credential (mounted read-only) for a
# single refreshed bearer, so parallel pi workers never race on token exchange.
FROM python:3.12-slim

ARG HEADROOM_VERSION=0.30.0
RUN pip install --no-cache-dir "headroom-ai[proxy]==${HEADROOM_VERSION}"

COPY --chmod=0755 launch.sh /usr/local/bin/bw-headroom-launch

# uid 1000 matches the workspace user (bw) that owns the mounted auth.json.
RUN useradd --uid 1000 --create-home headroom
USER headroom

ENTRYPOINT ["bw-headroom-launch"]
