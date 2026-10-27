FROM docker:29-cli AS client
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends bash shellcheck ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=client /usr/local/bin/docker /usr/local/bin/docker
COPY --from=client /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins
COPY tests/requirements.txt /tmp/ndm-test-requirements.txt
RUN pip install --no-cache-dir -r /tmp/ndm-test-requirements.txt
WORKDIR /workspace
ENV PYTHONPATH=/workspace
