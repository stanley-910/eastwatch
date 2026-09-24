# deploy/workspace/Dockerfile
FROM node:22.14.0-bookworm

ARG PI_VERSION=0.80.7
ARG GLAB_VERSION=1.53.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl fzf git jq openssh-client python3 python3-pip python3-venv tmux zsh \
    && rm -rf /var/lib/apt/lists/*
RUN npm install --global "@earendil-works/pi-coding-agent@${PI_VERSION}"
RUN curl --fail --location --silent --show-error \
      "https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/packages/generic/glab/${GLAB_VERSION}/glab_${GLAB_VERSION}_linux_amd64.tar.gz" \
      | tar -xz -C /usr/local/bin --strip-components=1 bin/glab

RUN groupmod --new-name bw node \
    && usermod --login bw --home /home/bw --move-home --shell /bin/zsh node
COPY --chown=bw:bw . /opt/eastwatch
COPY --chown=bw:bw deploy/workspace/skills /opt/eastwatch/skills
RUN python3 -m venv /opt/eastwatch/.venv \
    && /opt/eastwatch/.venv/bin/pip install --no-cache-dir /opt/eastwatch

ENV PATH="/opt/eastwatch/.venv/bin:/usr/local/bin:/usr/bin:/bin"
ENV BW_WORKSPACE_ROOT=/home/bw
# pi resolves its agent dir (auth.json, models.json) from this; without it,
# interactive pi via docker exec falls back to ~/.pi/agent, which has no
# headroom-copilot provider definition.
ENV PI_CODING_AGENT_DIR=/home/bw/.config/pi/agent
USER bw
WORKDIR /home/bw
ENTRYPOINT ["bw-runner"]
