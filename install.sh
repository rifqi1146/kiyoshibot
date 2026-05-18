#!/usr/bin/env bash
set -e

echo "Kiyoshi Bot Installer"
echo

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root:"
  echo "sudo bash $0"
  exit 1
fi

# Detect Package Manager
if command -v apt-get >/dev/null 2>&1; then
    PM="apt"
elif command -v dnf >/dev/null 2>&1; then
    PM="dnf"
elif command -v yum >/dev/null 2>&1; then
    PM="yum"
elif command -v pacman >/dev/null 2>&1; then
    PM="pacman"
elif command -v zypper >/dev/null 2>&1; then
    PM="zypper"
elif command -v apk >/dev/null 2>&1; then
    PM="apk"
else
    echo "Unsupported package manager. Please install dependencies manually."
    exit 1
fi

echo "[1/6] Installing system dependencies using $PM..."

case "$PM" in
    apt)
        apt-get update
        apt-get install -y python3 python3-venv python3-pip git ffmpeg curl unzip build-essential libjpeg-dev zlib1g-dev cmake libssl-dev gperf
        ;;
    dnf|yum)
        $PM install -y epel-release || true
        $PM install -y python3 python3-pip git ffmpeg curl unzip gcc gcc-c++ make libjpeg-turbo-devel zlib-devel cmake openssl-devel gperf
        ;;
    pacman)
        pacman -Sy --noconfirm --needed python python-pip git ffmpeg curl unzip base-devel libjpeg-turbo zlib cmake openssl gperf
        ;;
    zypper)
        zypper refresh
        zypper install -y python3 python3-pip git ffmpeg curl unzip gcc gcc-c++ make libjpeg8-devel zlib-devel cmake libopenssl-devel gperf
        ;;
    apk)
        apk update
        apk add python3 py3-pip git ffmpeg curl unzip build-base jpeg-dev zlib-dev cmake openssl-dev gperf
        ;;
esac

echo
echo "[2/6] Installing Node.js..."

if ! command -v node >/dev/null 2>&1; then
    case "$PM" in
        apt)
            curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
            apt-get install -y nodejs
            ;;
        dnf|yum)
            curl -fsSL https://rpm.nodesource.com/setup_20.x | bash -
            $PM install -y nodejs
            ;;
        pacman)
            pacman -S --noconfirm --needed nodejs npm
            ;;
        zypper)
            zypper install -y nodejs npm
            ;;
        apk)
            apk add nodejs npm
            ;;
    esac
    echo "✔ Node.js installed"
else
    echo "✔ Node.js already installed"
fi

echo
echo "[3/6] Installing Deno..."

if ! command -v deno >/dev/null 2>&1; then
  curl -fsSL https://deno.land/install.sh | sh
  ln -sf /root/.deno/bin/deno /usr/local/bin/deno
  echo "✔ Deno installed"
else
  echo "✔ Deno already installed"
fi

echo
echo "[4/6] Installing Speedtest Ookla..."

if ! command -v speedtest >/dev/null 2>&1; then
  ARCH=$(uname -m)

  case "$ARCH" in
    x86_64)
      SPEEDTEST_URL="https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-x86_64.tgz"
      ;;
    aarch64|arm64)
      SPEEDTEST_URL="https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-aarch64.tgz"
      ;;
    *)
      echo "Unsupported architecture for Speedtest Ookla: $ARCH"
      echo "Skipping Speedtest installation..."
      SPEEDTEST_URL=""
      ;;
  esac

  if [[ -n "$SPEEDTEST_URL" ]]; then
      curl -L "$SPEEDTEST_URL" | tar zx
      mv speedtest /usr/local/bin/speedtest || mv speedtest /usr/bin/speedtest
      chmod +x /usr/local/bin/speedtest 2>/dev/null || chmod +x /usr/bin/speedtest
      echo "✔ Speedtest installed"
  fi
else
  echo "✔ Speedtest already installed"
fi

echo
read -rp "Do you want to build local Telegram Bot API? [Y/N]: " BUILD_LOCAL_BOT_API
BUILD_LOCAL_BOT_API=$(echo "$BUILD_LOCAL_BOT_API" | tr '[:lower:]' '[:upper:]')

if [[ "$BUILD_LOCAL_BOT_API" == "Y" ]]; then
  echo
  echo "[5/6] Building local Telegram Bot API..."

  if [ ! -d "telegram-bot-api" ]; then
    git clone --recursive https://github.com/tdlib/telegram-bot-api.git
  else
    echo "✔ telegram-bot-api source already exists"
  fi

  cd telegram-bot-api

  if [ ! -d "build" ]; then
    mkdir build
  fi

  cd build
  cmake -DCMAKE_BUILD_TYPE=Release ..
  cmake --build . --target install -j"$(nproc)"
  cd ../..

  echo "✔ Local Telegram Bot API build successfully"
else
  echo
  echo "[5/6] Skipping local Telegram Bot API build..."
fi

echo
echo "[6/6] Creating virtual environment..."

if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
else
    PYTHON_CMD="python"
fi

if [ ! -d "venv" ]; then
  $PYTHON_CMD -m venv venv
fi

source venv/bin/activate

echo
echo "[7/7] Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt
pip install "scrapling[fetchers]"
scrapling install

deactivate

echo
echo "Done!"
echo
echo "Next steps:"
echo "1. nano .env"
echo "2. Fill BOT_TOKEN and API keys"
if [[ "$BUILD_LOCAL_BOT_API" == "Y" ]]; then
  echo "3. If using local Bot API, also fill API_ID and API_HASH in .env"
fi
echo "4. Run:"
echo "   source venv/bin/activate"
echo "   python main.py"
echo
echo "Happy"