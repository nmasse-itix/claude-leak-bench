# claude-code-redact (rdx), built from a local checkout used as the build context.
#   podman build -f containers/rdx.Containerfile -t leak-bench-rdx:<commit> ~/git/claude-code-redact
ARG FEDORA=44
FROM registry.fedoraproject.org/fedora-minimal:${FEDORA}
RUN microdnf -y install --setopt=install_weak_deps=0 python3 && microdnf clean all
COPY . /src
RUN python3 -m venv /app && /app/bin/pip install --no-cache-dir /src && rm -rf /src
ENV PATH=/app/bin:$PATH HOME=/tmp
USER 1000:1000
