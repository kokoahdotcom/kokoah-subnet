# The sealed machine image (specification section 16): the trusted harness, the sandbox and nothing else.
# Build with deploy/agent/build.sh: a reproducible build from a clean export of a commit, so the same commit always
# gives the same digest and anyone can rebuild an approved image and compare. Its digest is what validators approve
# (src/kokoah_subnet/agent/images.py, which is left out of the image so that approving an image does not change it).
# Everything that affects the guarantees is in the image: the code, the pinned packages, and image.json (builder,
# fee). Only the start settings listed in the launch policy below can be passed in.
FROM python@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY deploy/agent/requirements.txt /app/requirements.txt
# no compiled files (they hold install times) and no account tools (they stamp the build date): same bytes every build
RUN pip install --require-hashes --only-binary :all: --no-compile -r /app/requirements.txt \
    && echo 'agent:x:10002:10002::/home/agent:/usr/sbin/nologin' >> /etc/passwd \
    && echo 'agent:x:10002:' >> /etc/group \
    && mkdir -p /home/agent /run/kokoah /etc/kokoah-agent \
    && chown 10002:10002 /home/agent \
    && find / -xdev -perm /6000 -type f -exec chmod a-s {} +
COPY src/kokoah_subnet/__init__.py /app/kokoah_subnet/__init__.py
COPY src/kokoah_subnet/agent /app/kokoah_subnet/agent
ARG IMAGE_SETTINGS=deploy/agent/image.json
COPY ${IMAGE_SETTINGS} /etc/kokoah-agent/image.json
ENV PYTHONPATH=/app

LABEL "tee.launch_policy.allow_env_override"="KOKOAH_AGENT_SUBMISSION,KOKOAH_AGENT_BUNDLE_KEY,KOKOAH_AGENT_NETUID,KOKOAH_AGENT_HOTKEY,KOKOAH_AGENT_MASTER,KOKOAH_AGENT_SECRET,KOKOAH_AGENT_PERMIT,KOKOAH_AGENT_RESTORE,KOKOAH_AGENT_NETWORK"
LABEL "tee.launch_policy.log_redirect"="debugonly"
LABEL "tee.launch_policy.monitoring_memory_allow"="always"
EXPOSE 8080
ENTRYPOINT ["python", "-m", "kokoah_subnet.agent.supervisor"]
