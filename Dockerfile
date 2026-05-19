FROM python:3.13-slim

ENV TZ=Asia/Jakarta
ENV PATH="/root/.deno/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl unzip git wget gnupg2 tar build-essential nodejs \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && curl -fsSL https://deno.land/install.sh | sh \
    && ARCH=$(uname -m) && if [ "$ARCH" = "x86_64" ]; then URL="https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-x86_64.tgz"; elif [ "$ARCH" = "aarch64" ]; then URL="https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-aarch64.tgz"; fi \
    && if [ -n "$URL" ]; then curl -L "$URL" | tar zx -C /usr/local/bin speedtest; fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "scrapling[fetchers]" \
    && scrapling install

COPY . .

CMD ["python", "bot.py"]
