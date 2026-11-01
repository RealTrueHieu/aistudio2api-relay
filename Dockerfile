FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir cryptography

WORKDIR /app

# Download aistudio2api linux-amd64
RUN curl -fsSL -o aistudio2api.tar.gz https://github.com/Mag1cFall/AIStudio2API/releases/download/v0.2.18/aistudio2api-v0.2.18-linux-amd64.tar.gz \
    && tar -xzf aistudio2api.tar.gz \
    && rm -f aistudio2api.tar.gz \
    && if [ -d aistudio2api-v0.2.18-linux-amd64 ]; then mv aistudio2api-v0.2.18-linux-amd64/* . && rm -rf aistudio2api-v0.2.18-linux-amd64; fi \
    && chmod +x aistudio2api

COPY rotator.py /app/rotator.py
COPY bundle.enc /app/bundle.enc
COPY server.py /app/server.py

EXPOSE 10000

ENTRYPOINT ["python3", "-u", "/app/server.py"]
