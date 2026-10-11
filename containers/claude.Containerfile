# Claude Code, pinned, nothing else: bash + coreutils for the scenarios.
#   podman build -f containers/claude.Containerfile --build-arg CLAUDE_VERSION=2.1.283 -t leak-bench-claude:2.1.283 .
ARG FEDORA=44

FROM registry.fedoraproject.org/fedora-minimal:${FEDORA} AS fetch
ARG CLAUDE_VERSION
ARG PLATFORM=linux-x64
RUN microdnf -y install --setopt=install_weak_deps=0 jq && microdnf clean all
RUN set -eu; \
    test -n "${CLAUDE_VERSION}" || { echo "CLAUDE_VERSION build arg required" >&2; exit 1; }; \
    base="https://downloads.claude.ai/claude-code-releases/${CLAUDE_VERSION}"; \
    sum=$(curl -fsSL "$base/manifest.json" | jq -er ".platforms[\"${PLATFORM}\"].checksum"); \
    curl -fsSL -o /claude "$base/${PLATFORM}/claude"; \
    echo "$sum  /claude" | sha256sum -c -; \
    chmod 0755 /claude

FROM registry.fedoraproject.org/fedora-minimal:${FEDORA}
ARG CLAUDE_VERSION
COPY --from=fetch /claude /usr/local/bin/claude
# uid/gid 1000: run with --userns=keep-id:uid=1000,gid=1000 so files in the
# bind-mounted project belong to the host user on both sides.
RUN echo 'bench:x:1000:1000::/home/bench:/bin/bash' >> /etc/passwd \
 && echo 'bench:x:1000:' >> /etc/group \
 && mkdir -p /home/bench/.claude /work && chown -R 1000:1000 /home/bench /work
USER bench
WORKDIR /work
ENV HOME=/home/bench \
    CLAUDE_CONFIG_DIR=/home/bench/.claude \
    DISABLE_AUTOUPDATER=1 \
    SHELL=/bin/bash
LABEL io.leak-bench.claude-version="${CLAUDE_VERSION}"
ENTRYPOINT ["/usr/local/bin/claude"]
