# The runtime image: the tribunal, its grounding tools, and a credential passed in at run time.
#
# This is the image that holds the API key. `Dockerfile.sandbox` is the one that runs
# model-written code, and it holds no key, no tribunal code and no network. Keeping them apart is
# a security property rather than packaging tidiness (docs/05-execution-sandbox.md,
# docs/08-packaging.md § Docker) -- one image can reach the internet with a credential, the
# other can execute untrusted code, and nothing is both.
#
#   docker build -t tribunal .
#   docker run --rm -e NVIDIA_API_KEY \
#     -v "$PWD:/code:ro" -v "$PWD/traces:/traces" \
#     tribunal run /code/examples/sql_injection.py --config /code/examples/nim.toml
#
# See docker-compose.yml for the same thing without the flag soup, and the README for the
# docker-out-of-docker trade-off that `--sandbox=docker` requires from inside here.

# -- build ------------------------------------------------------------------------------------

FROM python:3.11-slim AS build

# A virtualenv rather than --user or the system site-packages: it is one directory to copy
# into the runtime stage, which is what keeps the compiler and the pip cache out of the
# shipped image.
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN python -m venv /opt/venv

WORKDIR /src

# Dependencies resolve from the metadata alone, so they cache across every source edit. Copying
# the whole tree first would reinstall bandit on every prompt tweak.
COPY pyproject.toml constraints.txt README.md ./
COPY src/tribunal/__init__.py src/tribunal/__init__.py
RUN pip install -c constraints.txt .

# `all-providers` pulls the Anthropic and OpenAI SDKs (the latter also speaks to NVIDIA NIM).
# Default off: the core install runs `ground`, `doctor`, `replay` and `view` with no SDK at all,
# and an image that cannot reach a provider is a legitimate thing to ship.
ARG PROVIDERS=""
RUN if [ -n "$PROVIDERS" ]; then pip install -c constraints.txt ".[${PROVIDERS}]"; fi

COPY src/ src/
# --no-deps: everything is already resolved and pinned above, and a second resolution here
# could quietly pull a different `ruff` than constraints.txt named.
RUN pip install --no-deps --force-reinstall .

# -- runtime ----------------------------------------------------------------------------------

FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="tribunal" \
      org.opencontainers.image.description="An adversarial multi-agent code reviewer." \
      org.opencontainers.image.source="https://github.com/meghagarg/tribunal"

COPY --from=build /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Separate instructions rather than one continued ENV, because a comment inside a line
# continuation is a Dockerfile parser hazard -- BuildKit strips them, older builders fold
# them into the instruction. Not worth a layer's worth of risk to explain two variables.

# Inside a container the tribunal is already isolated from the host, so subprocess is the right
# default here. `--sandbox=docker` from in here needs the host docker socket, which grants
# host-root-equivalent access; opt in explicitly. See the README.
ENV TRIBUNAL_SANDBOX__MODE=subprocess

# The documented mount point. A run with nothing mounted there still works -- the trace just
# stays inside the container and goes away with it.
ENV TRIBUNAL_TRACE_DIR=/traces

# Unprivileged, and `nobody` already exists in Debian. /traces is the only writable path the
# tribunal needs, so it is the only one it owns.
RUN install -d -o 65534 -g 65534 /traces /home/nobody
USER 65534:65534
ENV HOME=/home/nobody

# Read-only by convention: the tribunal never writes to the code under review, and mounting it
# `:ro` is what the README and compose file both do.
WORKDIR /code
VOLUME ["/traces"]

# No credential is baked in, and none may be: an API key passed as a build arg lands in the
# image history, and one passed as a CLI flag lands in shell history *and* in
# `run_start.payload.argv` in the trace (docs/08 § Credentials). Both are passed as `-e`.

# `doctor` as the default command: the first thing anyone runs when it breaks, and it exits
# non-zero if a grounding tool is missing, which makes a broken image fail its own smoke test.
ENTRYPOINT ["tribunal"]
CMD ["doctor"]
