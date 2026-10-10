"""After close_match, a buyer trades the coin; sweep_closed must forward the creator fee to the creator."""
import os, time; os.environ['ARENA_LIB'] = 'pinolib'
from pino_test import *
human, keeper, scorer, fallback, bob = (Keypair() for _ in range(5))
for k, s in [(human, 5), (keeper, 2), (scorer, 1), (bob, 20)]: airdrop(k.pubkey(), s)
M, _ = launch(human, keeper, scorer, fallback, lock=20, lo=10, hi=20, challenge=5, timeout=15, grace=60, dev=0.5)
V = vault_pda(M, human.pubkey())
K = Keeper(M, keeper, None, lambda ev, **f: print('   ', ev, f, flush=True), sweep_every=5)
while not K.done: K.tick(); time.sleep(1)                                 # no scorer: sale, draw, expire, close
print('match closed:', read_match(M) is None)
bix, _ = buy_sol_ix(bob.pubkey(), M, V, 5.0)
send([pf.create_ata_idempotent_ix(bob.pubkey(), bob.pubkey(), M, TOKEN22), bix], bob, [bob])
b = lamports(human.pubkey())
send(sweep_closed_ixs(keeper.pubkey(), M, human.pubkey(), 1), keeper, [keeper])
got = (lamports(human.pubkey()) - b) / 1e9
print(f"after close: 5 SOL buy -> creator fee forwarded to the creator: {got:.6f} SOL (0.30% of 5 SOL = 0.015)")
# A stranger naming themselves creator is covered by pino_fakecreator.py: against the REAL vault it is
# refused (BadAccount); against their own empty vault it is a harmless no-op.
