FROM ollama/ollama:latest

# OpenShift runs containers as an arbitrary non-root UID with no /etc/passwd entry,
# so HOME defaults to /. Set HOME to /tmp (always writable) for runtime key generation,
# and store models in /models so they are baked into the image and world-readable.
ENV HOME=/tmp
ENV OLLAMA_MODELS=/models

RUN mkdir -p /models && chmod 777 /models && \
    mkdir -p /tmp/.ollama && chmod 777 /tmp/.ollama

# Pre-pull the model at build time
RUN ollama serve & sleep 5 && ollama pull gemma:2b

# Expose API port
EXPOSE 11434

# Run Ollama
CMD ["serve"]