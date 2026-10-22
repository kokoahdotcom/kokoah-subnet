# The sealed machine image (specification v6, section 16): the trusted harness, the sandbox and nothing else.
# Build from the repository root:  docker build -f deploy/agent/Dockerfile -t kokoah-agent .
# Its content digest is what validators approve (src/kokoah_subnet/agent/images.py). Everything that affects the
# guarantees is in the image: the code, the pinned packages, and image.json (builder, fee). Only the start settings listed in the launch policy below can be passed in.
FROM python@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY deploy/agent/requirements.txt /app/requirements.txt
RUN pip install --require-hashes --only-binary :all: -r /app/requirements.txt \
    && useradd --uid 10002 --user-group --create-home --shell /usr/sbin/nologin agent \
    && find / -xdev -perm /6000 -type f -exec chmod a-s {} + \
    && mkdir -p /run/kokoah /etc/kokoah-agent
COPY src/kokoah_subnet/__init__.py /app/kokoah_subnet/__init__.py
COPY src/kokoah_subnet/agent /app/kokoah_subnet/agent
ARG IMAGE_SETTINGS=deploy/agent/image.json
COPY ${IMAGE_SETTINGS} /etc/kokoah-agent/image.json
ENV PYTHONPATH=/app

LABEL "tee.launch_policy.allow_env_override"="KOKOAH_AGENT_SERVICE,KOKOAH_AGENT_SUBMISSION,KOKOAH_AGENT_BUNDLE_KEY,KOKOAH_AGENT_NETUID,KOKOAH_AGENT_HOTKEY,KOKOAH_AGENT_MASTER,KOKOAH_AGENT_NETWORK"
LABEL "tee.launch_policy.log_redirect"="debugonly"
LABEL "tee.launch_policy.monitoring_memory_allow"="always"
EXPOSE 8080
ENTRYPOINT ["python", "-m", "kokoah_subnet.agent.supervisor"]
