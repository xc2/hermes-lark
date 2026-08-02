ARG HERMES_AGENT_IMAGE=nousresearch/hermes-agent@sha256:f59eb17c55f90409bb805525b7c2bd12dcd61355ebd3d2604272bed5dc597b67
FROM ${HERMES_AGENT_IMAGE}

COPY --chown=hermes:hermes . /opt/hermes-lark

RUN uv pip install \
      --no-cache \
      --python /opt/hermes/.venv/bin/python \
      /opt/hermes-lark \
    && /opt/hermes/.venv/bin/python -c \
      "from importlib.metadata import entry_points, version; expected={'hermes-agent':'0.19.1','hermes-lark':'0.1.0','aiohttp':'3.14.1','lark-oapi':'1.6.8','qrcode':'7.4.2','requests-toolbelt':'1.0.0','websockets':'15.0.1'}; assert {name: version(name) for name in expected} == expected; assert {ep.name: ep.value for ep in entry_points(group='hermes_agent.plugins')}['platforms/feishu'] == 'hermes_lark'"
