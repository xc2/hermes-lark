ARG HERMES_AGENT_IMAGE=nousresearch/hermes-agent@sha256:9c5561579f3ffdf333401797094ac3a743738d6c25d40f4331c7bf77b694238d
FROM ${HERMES_AGENT_IMAGE}

COPY --chown=hermes:hermes . /opt/hermes-lark

# Hermes Agent seals its own venv. tests/e2e/configure_gateway.py installs this
# checkout as a directory plugin and Hermes' package manager prepares its
# dependencies, the same path a production install takes.
RUN cd /opt/hermes \
    && /opt/hermes/.venv/bin/python -c \
      "import pathlib, tomllib, hermes_yaml; from hermes_cli.plugins_manifest import requires_hermes_error; expected={'hermes-lark':'1.0.0'}; root=pathlib.Path('/opt/hermes-lark'); manifest=hermes_yaml.safe_load((root / 'plugin.yaml').read_text()); project=tomllib.loads((root / 'pyproject.toml').read_text())['project']; assert {'hermes-lark': project['version']} == expected == {'hermes-lark': str(manifest['version'])}; assert requires_hermes_error(manifest) is None, requires_hermes_error(manifest)"
