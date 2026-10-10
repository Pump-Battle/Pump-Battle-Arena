"""Close the launch's address lookup table (refunds its ~0.009 SOL rent to the deploy wallet).
Run ~4 minutes after canary_launch.py deactivated it; retries until the chain allows the close."""
import json, os, sys, time, glob
for line in open(os.path.expanduser('~/.config/arena/keeper.env')):
    k, _, v = line.strip().partition('='); os.environ.setdefault(k, v)
os.environ['FIGHT_RPC'] = os.environ['ARENA_RPC']; os.environ['FIGHT_CU_PRICE'] = os.environ.get('ARENA_CU_PRICE', '200000')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'client'))
from chainlib import *
deploy = Keypair.from_bytes(bytes(json.load(open(os.path.expanduser('~/.config/arena/deploy.json')))))
tables = []
for f in glob.glob(os.path.expanduser('~/.local/state/arena/canary_*.jsonl')):
    for l in open(f):
        r = json.loads(l)
        if r['ev'] == 'lookup_table': tables.append(r['table'])
        if r['ev'] == 'lookup_table_closed': tables = [t for t in tables if t != r['table']]
if not tables: sys.exit('no open lookup tables recorded')
for t in tables:
    if raw_account(P(t)) is None: print('already closed:', t); continue
    for _ in range(40):
        try:
            sig = send(close_lookup_table_ixs(deploy.pubkey(), P(t), 'close'), deploy, [deploy]); print('closed', t, sig); break
        except Exception as e:
            print('not yet (', str(e)[:60], '); retrying in 15 s'); time.sleep(15)
print('deploy wallet:', lamports(deploy.pubkey()) / 1e9, 'SOL')
