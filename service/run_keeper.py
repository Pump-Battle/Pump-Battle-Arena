"""Keeper service: runs one match (or several) to completion and keeps sweeping fees afterwards.

    run_keeper.py --mint <mint> [--mint <mint> ...]      keys from ~/.config/arena/{keeper,scorer}.json
Env: ARENA_RPC (http), ARENA_CU_PRICE (micro-lamports/CU, mainnet only), ARENA_LIB (pinolib). Logs JSON lines to
~/.local/state/arena/keeper.jsonl and keeps ~/.local/state/arena/status.json current for `arena-status`.
"""
import os, sys, json, time, argparse
ap = argparse.ArgumentParser(); ap.add_argument('--mint', action='append', required=True); ap.add_argument('--sweep-every', type=int, default=20)
a = ap.parse_args()
os.environ.setdefault('ARENA_LIB', 'pinolib')
if os.environ.get('ARENA_RPC'): os.environ['FIGHT_RPC'] = os.environ['ARENA_RPC']
if os.environ.get('ARENA_CU_PRICE'): os.environ['FIGHT_CU_PRICE'] = os.environ['ARENA_CU_PRICE']
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'client'))
from keeper import *
ST = os.path.expanduser('~/.local/state/arena'); os.makedirs(ST, exist_ok=True)
log_f = open(f'{ST}/keeper.jsonl', 'a')
load = lambda n: Keypair.from_bytes(bytes(json.load(open(os.path.expanduser(f'~/.config/arena/{n}.json')))))
keeper, scorer = load('keeper'), load('scorer')
def mklog(mint):
    def log(ev, **f):
        row = dict(t=int(time.time()), mint=str(mint), ev=ev, **f); log_f.write(json.dumps(row, default=str) + '\n'); log_f.flush(); print(json.dumps(row, default=str), flush=True)
    return log
ks = {m: Keeper(P(m), keeper, scorer, mklog(m), sweep_every=a.sweep_every) for m in a.mint}
while True:
    status = dict(t=int(time.time()), keeper=str(keeper.pubkey()), keeper_sol=lamports(keeper.pubkey()) / 1e9, matches={})
    for m, k in list(ks.items()):
        try:
            if not k.done: k.tick()
            st = read_match(P(m))
            status['matches'][m] = dict(done=k.done, **({kk: (str(v) if isinstance(v, Pubkey) else v) for kk, v in st.items()} if st else {'closed': True}))
        except Exception as e:
            status['matches'][m] = dict(error=str(e)[:200]); k.log('tick_error', err=str(e)[:200])
    json.dump(status, open(f'{ST}/status.json.tmp', 'w'), default=str); os.replace(f'{ST}/status.json.tmp', f'{ST}/status.json')
    if all(k.done for k in ks.values()):
        break
    time.sleep(3)
