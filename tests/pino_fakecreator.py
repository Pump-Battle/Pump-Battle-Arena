"""Can anyone redirect a closed match's creator fees? Eve names herself the creator, against the REAL vault."""
import os, time, struct; os.environ['ARENA_LIB'] = 'pinolib'
from pino_test import *
human, keeper, scorer, fallback, bob, eve = (Keypair() for _ in range(6))
for k, s in [(human, 5), (keeper, 2), (scorer, 1), (bob, 20), (eve, 2)]: airdrop(k.pubkey(), s)
M, _ = launch(human, keeper, scorer, fallback, lock=20, lo=10, hi=20, challenge=5, timeout=15, grace=60, dev=0.5)
V, vb = vault_pda(M, human.pubkey(), True)
K = Keeper(M, keeper, None, lambda *a, **k: None, sweep_every=5)
while not K.done: K.tick(); time.sleep(1)
bix, _ = buy_sol_ix(bob.pubkey(), M, V, 5.0)
send([pf.create_ata_idempotent_ix(bob.pubkey(), bob.pubkey(), M, TOKEN22), bix], bob, [bob])
real = sweep_closed_ixs(eve.pubkey(), M, human.pubkey(), 1)[-1]                 # the honest instruction...
accts = list(real.accounts); accts[1] = AccountMeta(eve.pubkey(), False, True)   # ...with Eve as the "creator"
for vbump in (vb, ):                                                             # and the real vault's bump
    ix = Instruction(real.program_id, bytes([8, vbump]) + bytes(real.data[2:]), accts)
    e0 = lamports(eve.pubkey())
    try:
        send([ix], eve, [eve]); print(f'FAIL: went through; Eve gained {(lamports(eve.pubkey()) - e0)/1e9:+.6f}')
    except Exception as e:
        print("pass: Eve as creator against the real vault is refused (BadAccount):", "'Custom': 14}" in str(e))
# and the earlier case: Eve's OWN derived vault touches nothing of the real coin
e0, v0 = lamports(eve.pubkey()), lamports(V)
try: send(sweep_closed_ixs(eve.pubkey(), M, eve.pubkey(), 1), eve, [eve])
except Exception as e: print('(own-vault call refused:', str(e)[:60], ')')
print(f"Eve's own-vault call: Eve {(lamports(eve.pubkey()) - e0)/1e9:+.6f} SOL (just her tx fee), real vault {(lamports(V) - v0)/1e9:+.6f} SOL")
h0 = lamports(human.pubkey()); send(sweep_closed_ixs(keeper.pubkey(), M, human.pubkey(), 1), keeper, [keeper])
print(f"the real creator still collects: +{(lamports(human.pubkey()) - h0)/1e9:.6f} SOL")
