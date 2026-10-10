#!/usr/bin/env bash
# Battle Bot Arena sim chain: a SECOND solana-test-validator, separate from the votekeep
# testbed service (8899). Clones the real pump.fun / PumpSwap / fee / mayhem programs and
# their global accounts from mainnet. Ledger is disposable and reset on every start.
set -euo pipefail
export PATH="$HOME/.local/share/solana/install/active_release/bin:$PATH"
IP=$(ip -4 -br addr show enp1s0 | awk "{print \$3}" | cut -d/ -f1)
# --reset wipes the old ledger first, so its space counts as free
AVAIL=$(df -BG --output=avail "$HOME" | tail -1 | tr -dc 0-9)
OLD=$(du -s -BG "$HOME/fight-ledger" 2>/dev/null | cut -f1 | tr -dc 0-9)
[ $(( ${AVAIL:-0} + ${OLD:-0} )) -ge 10 ] || { echo "only ${AVAIL}G free (+${OLD:-0}G reclaimable)" >&2; exit 1; }
mapfile -t CLONES < "$HOME/fight/clone.args"
exec solana-test-validator --ledger "$HOME/fight-ledger" --reset --limit-ledger-size 2000000 \
  --rpc-port 18899 --faucet-port 19900 --gossip-port 18001 --dynamic-port-range 18002-18100 \
  --bind-address "$IP" "${CLONES[@]}"
