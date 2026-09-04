# Ephemeral GitHub Actions runner for the DOT Oracle CI job.
#
# The container is started by the webhook Lambda with a just-in-time runner
# configuration, runs exactly one job, and exits. It is deliberately thin: the
# workflow installs its own Python via actions/setup-python, so the image only
# has to provide the runner, git, and the shared libraries those Python builds
# link against.
FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
# Pin this to a released actions/runner version for reproducible images; "latest"
# resolves at build time, which is convenient but makes rebuilds non-deterministic.
ARG RUNNER_VERSION=latest
ARG TARGETARCH=amd64

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        jq \
        libicu74 \
        liblttng-ust1 \
        locales \
        openssh-client \
        sudo \
        tar \
        unzip \
        zstd \
    # Shared libraries the actions/setup-python builds link against.
    && apt-get install -y --no-install-recommends \
        libbz2-1.0 \
        libffi8 \
        libgdbm6t64 \
        liblzma5 \
        libncursesw6 \
        libreadline8t64 \
        libsqlite3-0 \
        libssl3t64 \
        uuid-runtime \
        zlib1g \
    && locale-gen en_US.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    RUNNER_TOOL_CACHE=/opt/hostedtoolcache \
    AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache \
    DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=0

RUN useradd --create-home --shell /bin/bash runner \
    && mkdir -p /opt/hostedtoolcache /home/runner/actions-runner \
    && chown -R runner:runner /opt/hostedtoolcache /home/runner

USER runner
WORKDIR /home/runner/actions-runner

RUN set -eux; \
    version="${RUNNER_VERSION}"; \
    if [ "$version" = "latest" ]; then \
        version="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name | sed 's/^v//')"; \
    fi; \
    case "${TARGETARCH}" in \
        amd64) arch=x64 ;; \
        arm64) arch=arm64 ;; \
        *) echo "unsupported TARGETARCH ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o runner.tar.gz \
        "https://github.com/actions/runner/releases/download/v${version}/actions-runner-linux-${arch}-${version}.tar.gz"; \
    tar xzf runner.tar.gz; \
    rm runner.tar.gz; \
    echo "${version}" > /home/runner/.runner-version

# installdependencies.sh needs root; the runner itself must not run as root.
USER root
RUN /home/runner/actions-runner/bin/installdependencies.sh && rm -rf /var/lib/apt/lists/*
USER runner

COPY --chown=runner:runner entrypoint.sh /home/runner/entrypoint.sh

ENTRYPOINT ["/home/runner/entrypoint.sh"]
