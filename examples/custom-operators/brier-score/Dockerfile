# Build from the approved SDK image pinned to its digest, not a moving tag.
ARG SDK_IMAGE
FROM ${SDK_IMAGE}
WORKDIR /opt/research-brier
COPY brier_operator.py serve.py ./
ENTRYPOINT ["python", "/opt/research-brier/serve.py"]
# Same image supports Docker --once and FC HTTP /invoke on port 8080.
