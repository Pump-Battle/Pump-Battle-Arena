#!/usr/bin/env bash
# Set up a fresh Ubuntu/Debian host (e.g. a GCP e2-micro) to run the keeper. Run as the user that will own it.
#   bash setup-host.sh            then copy keeper.json / scorer.json into ~/.config/arena and fill keeper.env
set -euo pipefail
sudo apt-get update -q && sudo apt-get install -y -q python3-venv python3-pip git
[ -d ~/pump-battle-arena ] || git clone https://github.com/Pump-Battle/Pump-Battle-Arena.git ~/pump-battle-arena
cd ~/pump-battle-arena && git pull -q
python3 -m venv ~/arena-venv && ~/arena-venv/bin/pip install -q "solders==0.29.0" base58 httpx
mkdir -p ~/.config/arena ~/.config/systemd/user ~/.local/state/arena && chmod 700 ~/.config/arena
[ -f ~/.config/arena/keeper.env ] || printf 'ARENA_RPC=https://REPLACE-with-your-rpc-url\nARENA_CU_PRICE=200000\nARENA_LIB=pinolib\n' > ~/.config/arena/keeper.env
sed "s#/usr/bin/env python3 -u#$HOME/arena-venv/bin/python -u#" service/arena-keeper@.service > ~/.config/systemd/user/arena-keeper@.service
systemctl --user daemon-reload
sudo loginctl enable-linger "$USER"     # user services keep running after logout
ln -sf ~/pump-battle-arena/service/arena-status ~/.local/bin/arena-status 2>/dev/null || true
echo "done. Next: put keeper.json and scorer.json in ~/.config/arena/, set ARENA_RPC in keeper.env, then"
echo "  systemctl --user start arena-keeper@<mint>.service && arena-status"
