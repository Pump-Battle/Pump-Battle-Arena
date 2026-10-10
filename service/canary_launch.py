"""Mainnet canary: launch ONE tiny match on the deployed arena program.

    python canary_launch.py --uri <metadata url> --name X --symbol Y [--dev-sol 0.01] [--lock-min 15]

Keys: ~/.config/arena/deploy.json (pays), keeper.json, scorer.json, program-keypair.json (program id),
      ~/.config/arena/mint-*.json (the vanity mint, ends in 'pump'). RPC from ~/.config/arena/keeper.env.
Steps: fund keeper + scorer from the deploy wallet -> lookup table -> ONE v0 tx (create coin with the vault as
creator, contract dev buy, lock) -> deactivate the table. Prints the mint and the keeper command to run next.
Everything is logged to ~/.local/state/arena/canary_<mint>.jsonl.
"""
import argparse, glob, json, os, sys, time
ap = argparse.ArgumentParser()
ap.add_argument('--uri', required=True); ap.add_argument('--name', required=True); ap.add_argument('--symbol', required=True)
ap.add_argument('--dev-sol', type=float, default=0.01); ap.add_argument('--lock-min', type=float, default=15)
ap.add_argument('--grace-s', type=int, default=120); ap.add_argument('--challenge-s', type=int, default=180); ap.add_argument('--timeout-s', type=int, default=1500)
ap.add_argument('--tier', choices=['blitz', 'standard', 'hour'], help='preset match length: blitz 5 min, standard 15 min, hour 65 min')
a = ap.parse_args()
TIERS = dict(blitz=dict(lock_min=5, grace_s=30, challenge_s=60, timeout_s=300),        # bell in the last minute
             standard=dict(lock_min=15, grace_s=120, challenge_s=180, timeout_s=1500),  # bell in the last 2.3 min
             hour=dict(lock_min=65, grace_s=300, challenge_s=3600, timeout_s=7200))    # bell between minute 55 and 65
if a.tier:
    for k, v in TIERS[a.tier].items(): setattr(a, k, v)
for line in open(os.path.expanduser('~/.config/arena/keeper.env')):
    k, _, v = line.strip().partition('='); os.environ.setdefault(k, v)
os.environ['FIGHT_RPC'] = os.environ['ARENA_RPC']; os.environ['FIGHT_CU_PRICE'] = os.environ.get('ARENA_CU_PRICE', '200000'); os.environ['ARENA_LIB'] = 'pinolib'
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'client'))
from pinolib import *
import pinolib
load = lambda p: Keypair.from_bytes(bytes(json.load(open(os.path.expanduser(p)))))
deploy, keeper, scorer = load('~/.config/arena/deploy.json'), load('~/.config/arena/keeper.json'), load('~/.config/arena/scorer.json')
prog = load('~/.config/arena/program-keypair.json').pubkey()
assert prog == pinolib.PROG, f'pinolib.PROG {pinolib.PROG} != program keypair {prog}'
mints = sorted(glob.glob(os.path.expanduser('~/.config/arena/mint-*pump.json')))
if not mints: sys.exit('no vanity mint keypair in ~/.config/arena/ (mint-<addr>pump.json)')
mint_kp = load(mints[0]); M = mint_kp.pubkey()
assert str(M).endswith('pump'), M
ST = os.path.expanduser('~/.local/state/arena'); os.makedirs(ST, exist_ok=True)
out = open(f'{ST}/canary_{M}.jsonl', 'a')
def rec(ev, **f):
    row = dict(t=int(time.time()), ev=ev, **f); out.write(json.dumps(row, default=str) + '\n'); out.flush(); print(json.dumps(row, default=str), flush=True)
b0 = lamports(deploy.pubkey())
print(f'MAINNET  deploy {deploy.pubkey()} {b0/1e9:.4f} SOL | keeper {keeper.pubkey()} | scorer {scorer.pubkey()} | program {prog} | mint {M}')
assert raw_account(prog) is not None, 'program not deployed on this RPC'
# 1. fund the keeper (sweeps, sale, draw, claim, close: ~40 txs at 200k CU price) and the scorer (one tx)
need = {keeper.pubkey(): 0.03, scorer.pubkey(): 0.005}
ixs = [Instruction(SYSTEM, bytes([2, 0, 0, 0]) + int(s * LAMPORTS).to_bytes(8, 'little'), [AccountMeta(deploy.pubkey(), True, True), AccountMeta(k, False, True)])
       for k, s in need.items() if lamports(k) < int(s * LAMPORTS * 0.8)]
if ixs: rec('funded_roles', sig=send(ixs, deploy, [deploy]), keeper_sol=0.03, scorer_sol=0.005)
# 2. the launch: one v0 tx through a lookup table
lock = int(a.lock_min * 60); lo, hi = int(lock * 55 / 65), lock
launch = launch_ixs(deploy.pubkey(), M, a.name, a.symbol, a.dev_sol, lock, lo, hi, a.challenge_s, a.timeout_s, 5000, 2500,
                    scorer.pubkey(), deploy.pubkey(), keeper=keeper.pubkey(), grace=a.grace_s)
c = launch[0]; d = bytearray(c.data); off = 8
for _ in range(2): off += 4 + struct.unpack_from('<I', d, off)[0]
n = struct.unpack_from('<I', d, off)[0]; d[off:off + 4 + n] = struct.pack('<I', len(a.uri.encode())) + a.uri.encode()   # real metadata uri
launch[0] = Instruction(c.program_id, bytes(d), c.accounts)
sk = {deploy.pubkey(), M}
table = make_lookup_table(deploy, [x.pubkey for ix in launch for x in ix.accounts if x.pubkey not in sk] + [ix.program_id for ix in launch])
rec('lookup_table', table=table.key)
sig, size = send_v0(launch, deploy, [deploy, mint_kp], [table], cu=600_000)
m = read_match(M)
rec('launched', sig=sig, tx_bytes=size, mint=M, vault=vault_pda(M, deploy.pubkey()), cost_basis=m['cost_basis'] / 1e9, tokens_locked=m['tokens_locked'] / 1e12,
    unlock_at=time.strftime('%H:%M:%S EDT', time.localtime(m['unlock_ts'])), candle_window_s=[lo, hi], spent=(b0 - lamports(deploy.pubkey())) / 1e9)
send(close_lookup_table_ixs(deploy.pubkey(), table.key, 'deactivate'), deploy, [deploy], simulate_first=False)
rec('lookup_table_deactivated', note='close it ~4 min later to refund its rent: canary_close_table.py')
print(f'\nNEXT:  systemctl --user start arena-keeper@{M}.service   then   arena-status')
print(f'pump.fun: https://pump.fun/coin/{M}')
