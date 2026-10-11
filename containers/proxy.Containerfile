# The recording proxy: the only way out for the Claude Code container.
#   podman build -f containers/proxy.Containerfile -t leak-bench-proxy .
ARG FEDORA=44
FROM registry.fedoraproject.org/fedora-minimal:${FEDORA}
RUN microdnf -y install --setopt=install_weak_deps=0 python3 python3-aiohttp python3-zstandard \
 && microdnf clean all
COPY bench/proxy.py /app/proxy.py
USER 1000:1000
ENTRYPOINT ["python3", "/app/proxy.py"]
