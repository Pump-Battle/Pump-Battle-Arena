"""Tests for the Pinocchio arena contract on the sim chain (real pump.fun + PumpSwap).

    ARENA_LIB=pinolib python pino_test.py [S1 S2 S3 ATTACKS]

S1/S2/S3: the same three matches as hybrid_test.py, run by the keeper alone (plus close + post-close sweep).
ATTACKS: every refusal the contract is supposed to make, each expected to FAIL with its own error code.
"""
import os, sys, json, time
os.environ['ARENA_LIB'] = 'pinolib'
from keeper import *

ERR = dict(BadParams=1, BadCoin=2, NotFresh=3, BadTrade=4, NotWholeBag=5, StillLocked=6, AlreadySold=7, AlreadyDrawn=8,
           NotReady=9, Closed=10, WindowOpen=11, WrongRecipient=12, NotKeeper=13, BadAccount=14, Math=15)
RESULTS = []


def launch(human, keeper, scorer, fallback, lock, lo, hi, challenge=10, timeout=300, grace=20, dev=1.0):
    mint_kp = Keypair(); M = mint_kp.pubkey()
    ixs = launch_ixs(human.pubkey(), M, 'Battle Arena', 'FIGHT', dev, lock, lo, hi, challenge, timeout, 5000, 2500,
                     scorer.pubkey(), fallback.pubkey(), keeper=keeper.pubkey(), grace=grace)
    sk = {human.pubkey(), M}
    table = make_lookup_table(human, [a.pubkey for ix in ixs for a in ix.accounts if a.pubkey not in sk] + [ix.program_id for ix in ixs])
    _, size = send_v0(ixs, human, [human, mint_kp], [table])
    return M, size


def scenario(name, plan, lock, lo, hi, challenge=10, timeout=300, scorer_on=True, grace=20):
    print(f'\n================ {name} ================', flush=True)
    human, keeper, scorer, fallback = Keypair(), Keypair(), Keypair(), Keypair()
    names = sorted({a[1] for a in plan}); tr = {n: Keypair() for n in names}
    for k, sol in [(human, 5), (keeper, 2), (scorer, 1)] + [(tr[n], 160 if n == 'whale' else 30) for n in names]:
        airdrop(k.pubkey(), sol)
    start_bal = {n: lamports(tr[n].pubkey()) for n in names}; h0 = lamports(human.pubkey())
    events = []; T0 = [chain_time()]
    def log(ev, **f):
        row = dict(t=chain_time() - T0[0], ev=ev, **f); events.append(row); print('   ', json.dumps(row, default=str), flush=True)
    M, size = launch(human, keeper, scorer, fallback, lock, lo, hi, challenge, timeout, grace)
    V = vault_pda(M, human.pubkey()); m = read_match(M); T0[0] = m['start_ts']
    log('launched', tx_bytes=size, cost_basis=m['cost_basis'] / 1e9, tokens=m['tokens_locked'] / 1e12, vault_is_creator=curve(M).creator == V)
    K = Keeper(M, keeper, scorer if scorer_on else None, log, sweep_every=12)
    todo = sorted(plan)
    while not K.done:
        now = chain_time() - T0[0]
        while todo and todo[0][0] <= now:
            t, who, act, x = todo.pop(0); k = tr[who]; graduated = amm_pool(M) is not None
            try:
                if act == 'buy' and not graduated:
                    ix, _ = buy_sol_ix(k.pubkey(), M, V, x, slippage_bps=500)
                    send([pf.create_ata_idempotent_ix(k.pubkey(), k.pubkey(), M, TOKEN22), ix], k, [k])
                elif act == 'fill':
                    c = curve(M); fr, bb = fee_accounts(M)
                    send([pf.create_ata_idempotent_ix(k.pubkey(), k.pubkey(), M, TOKEN22),
                          pf.buy_v2_ix(k.pubkey(), M, V, TOKEN22, fr, bb, int(round(c.r_tok * 1e6)), 130 * LAMPORTS)], k, [k])
                elif act == 'buy':
                    send(amm_buy_exact_ixs(k.pubkey(), M, x), k, [k])
                else:
                    q = int(token_balance(k.pubkey(), M) * x)
                    send([sell_tokens_ix(k.pubkey(), M, V, q)] if not graduated else amm_sell_ixs(k.pubkey(), M, q), k, [k])
            except Exception as e:
                log('trade_failed', who=who, act=act, err=str(e).split(chr(10))[0][:120])
        K.tick(); time.sleep(1)
    closed = read_match(M) is None
    # post-close: fees keep flowing to the creator through sweep_closed
    if closed:
        b = lamports(human.pubkey())
        try:
            send(sweep_closed_ixs(keeper.pubkey(), M, human.pubkey(), 1), keeper, [keeper]); log('sweep_closed', to_creator=(lamports(human.pubkey()) - b) / 1e9)
        except Exception as e:
            log('sweep_closed_failed', err=str(e).split(chr(10))[0][:120])
    winner = next((e['winner'] for e in events if e['ev'] == 'posted'), None)
    wname = next((n for n in names if str(tr[n].pubkey()) == winner), None)
    res = dict(name=name, closed=closed, creator_pnl=(lamports(human.pubkey()) - h0) / 1e9, fallback_got=lamports(fallback.pubkey()) / 1e9,
               winner=wname, events=events)
    print(f"--- {name}: creator P&L {res['creator_pnl']:+.4f} SOL | match account closed {closed} | winner {wname} | fallback got {res['fallback_got']:.4f}")
    RESULTS.append(res)


def expect_fail(label, ixs, payer, signers, code=None):
    try:
        send(ixs, payer, signers)
        print(f'  FAIL  {label}: went through'); RESULTS.append(dict(attack=label, ok=False)); return False
    except Exception as e:
        msg = str(e).split(chr(10))[0]
        got = f"'Custom': {code}}}" in msg if code else True
        print(f"  {'pass' if got else 'FAIL'}  {label}: refused ({msg[msg.find('{'):][:70]})"); RESULTS.append(dict(attack=label, ok=got, err=msg[:160]))
        return got


def attacks():
    print('\n================ ATTACKS ================', flush=True)
    human, keeper, scorer, fallback, eve, bob = (Keypair() for _ in range(6))
    for k, s in [(human, 5), (keeper, 2), (scorer, 1), (eve, 50), (bob, 20)]: airdrop(k.pubkey(), s)
    # A1-A2: create_match must refuse a coin that already has buys, and a coin whose creator isn't the vault
    mint_kp = Keypair(); M = mint_kp.pubkey()
    ixs = launch_ixs(human.pubkey(), M, 'X', 'X', 0.5, 40, 30, 40, 5, 60, 5000, 2500, scorer.pubkey(), fallback.pubkey(), keeper=keeper.pubkey())
    V = vault_pda(M, human.pubkey())
    send([ixs[0]], human, [human, mint_kp])                                       # coin created, no match yet
    bix, _ = buy_sol_ix(eve.pubkey(), M, V, 0.2)
    send([pf.create_ata_idempotent_ix(eve.pubkey(), eve.pubkey(), M, TOKEN22), bix], eve, [eve])   # someone buys first
    t = make_lookup_table(human, [a.pubkey for a in ixs[2].accounts if a.pubkey != human.pubkey()] + [ixs[2].program_id])
    try:
        send_v0(ixs[1:], human, [human], [t]); print('  FAIL  A1 sniped coin accepted'); RESULTS.append(dict(attack='A1', ok=False))
    except Exception as e:
        ok = "'Custom': 3}" in str(e); print(f"  {'pass' if ok else 'FAIL'}  A1 create_match on a coin someone already bought: refused (NotFresh)"); RESULTS.append(dict(attack='A1 not fresh', ok=ok))
    mint2 = Keypair(); M2 = mint2.pubkey()
    send([create_v2_ix(M2, human.pubkey(), 'Y', 'Y')], human, [human, mint2])    # creator = human, not the vault
    ixs2 = launch_ixs(human.pubkey(), M2, 'Y', 'Y', 0.5, 40, 30, 40, 5, 60, 5000, 2500, scorer.pubkey(), fallback.pubkey(), keeper=keeper.pubkey())
    t2 = make_lookup_table(human, [a.pubkey for a in ixs2[2].accounts if a.pubkey != human.pubkey()] + [ixs2[2].program_id])
    try:
        send_v0(ixs2[1:], human, [human], [t2]); print('  FAIL  A2 accepted'); RESULTS.append(dict(attack='A2', ok=False))
    except Exception as e:
        ok = "'Custom': 2}" in str(e); print(f"  {'pass' if ok else 'FAIL'}  A2 create_match on a coin whose creator isn't the vault: refused (BadCoin)"); RESULTS.append(dict(attack='A2 bad coin', ok=ok))
    # a real match for the rest
    M, _ = launch(human, keeper, scorer, fallback, lock=30, lo=20, hi=30, challenge=15, timeout=200, grace=60, dev=1.0)
    V = vault_pda(M, human.pubkey()); m = read_match(M)
    bix, _ = buy_sol_ix(bob.pubkey(), M, V, 2.0)
    send([pf.create_ata_idempotent_ix(bob.pubkey(), bob.pubkey(), M, TOKEN22), bix], bob, [bob])
    expect_fail('A3 sale before unlock', settle_ixs(keeper.pubkey(), M, human.pubkey(), m['tokens_locked']), keeper, [keeper], ERR['StillLocked'])
    while chain_time() < m['unlock_ts']: time.sleep(1)
    expect_fail('A4 non-keeper triggers the sale during the grace period', settle_ixs(eve.pubkey(), M, human.pubkey(), m['tokens_locked']), eve, [eve], ERR['NotKeeper'])
    expect_fail('A5 partial sale (half the bag)', settle_ixs(keeper.pubkey(), M, human.pubkey(), m['tokens_locked'] // 2), keeper, [keeper], ERR['NotWholeBag'])
    # A6: sandwich. Eve dumps first; the keeper's sale carries a floor set BEFORE the dump, so it fails instead of filling cheap
    K = Keeper(M, keeper, scorer, lambda *a, **k: None)
    floor = K.floor(M, m['tokens_locked'])
    ex, _ = buy_sol_ix(eve.pubkey(), M, V, 10.0)
    send([pf.create_ata_idempotent_ix(eve.pubkey(), eve.pubkey(), M, TOKEN22), ex], eve, [eve])
    floor = K.floor(M, m['tokens_locked'])                                        # keeper quotes...
    send([sell_tokens_ix(eve.pubkey(), M, V, token_balance(eve.pubkey(), M))], eve, [eve])   # ...eve front-runs with a dump
    expect_fail('A6 sandwich: keeper sale with a floor after a front-run dump', settle_ixs(keeper.pubkey(), M, human.pubkey(), m['tokens_locked'], floor), keeper, [keeper])
    send(settle_ixs(keeper.pubkey(), M, human.pubkey(), m['tokens_locked'], K.floor(M, m['tokens_locked'])), keeper, [keeper], cu=700_000)
    print(f"  pass  A6b keeper re-quotes and sells at the honest price: proceeds {read_match(M)['proceeds']/1e9:.4f} SOL")
    expect_fail('A7 second sale', settle_ixs(keeper.pubkey(), M, human.pubkey(), m['tokens_locked']), keeper, [keeper], ERR['AlreadySold'])
    send([draw_ix(keeper.pubkey(), M)], keeper, [keeper])
    expect_fail('A8 second candle draw', [draw_ix(keeper.pubkey(), M)], keeper, [keeper], ERR['AlreadyDrawn'])
    expect_fail('A9 result posted by someone other than the scorer', [post_ix(eve.pubkey(), M, eve.pubkey(), 1, b'\x00' * 32)], eve, [eve], ERR['BadAccount'])
    # A10: donation. Eve sends SOL straight into the vault: it's split as fees (25% pot / 75% creator), nothing breaks
    send(sweep_ixs(keeper.pubkey(), M, human.pubkey(), 0), keeper, [keeper])      # clear anything already free
    pre = read_match(M)
    send([Instruction(SYSTEM, bytes([2, 0, 0, 0]) + (100_000_000).to_bytes(8, 'little'), [AccountMeta(eve.pubkey(), True, True), AccountMeta(V, False, True)])], eve, [eve])
    send(sweep_ixs(keeper.pubkey(), M, human.pubkey(), 0), keeper, [keeper])
    post = read_match(M)
    d_pot, d_paid = (post['prize_pot'] - pre['prize_pot']) / 1e9, (post['paid_creator'] - pre['paid_creator']) / 1e9
    ok = abs(d_pot - 0.025) < 1e-6 and abs(d_paid - 0.075) < 1e-6
    print(f"  {'pass' if ok else 'FAIL'}  A10 0.1 SOL donated into the vault: split {d_pot:.4f} to the pot / {d_paid:.4f} to the creator"); RESULTS.append(dict(attack='A10 donation', ok=ok))
    send([post_ix(scorer.pubkey(), M, bob.pubkey(), 123, b'\x11' * 32)], scorer, [scorer])
    expect_fail('A11 claim before the challenge window ends', [pay_ix('claim', eve.pubkey(), M, bob.pubkey())], eve, [eve], ERR['WindowOpen'])
    expect_fail('A12 expire while a result is posted', [pay_ix('expire', eve.pubkey(), M, fallback.pubkey())], eve, [eve], ERR['NotReady'])
    while chain_time() < read_match(M)['deadline']: time.sleep(1)
    expect_fail('A13 claim to a wallet that is not the winner', [pay_ix('claim', eve.pubkey(), M, eve.pubkey())], eve, [eve], ERR['WrongRecipient'])
    expect_fail('A14 close the match before the prize is paid', [close_ix(eve.pubkey(), M, human.pubkey())], eve, [eve], ERR['NotReady'])
    b = lamports(bob.pubkey()); send([pay_ix('claim', eve.pubkey(), M, bob.pubkey())], eve, [eve])
    print(f"  pass  A15 claim by a stranger pays the WINNER, not the caller: winner +{(lamports(bob.pubkey()) - b)/1e9:.4f} SOL")
    expect_fail('A16 double claim', [pay_ix('claim', eve.pubkey(), M, bob.pubkey())], eve, [eve], ERR['Closed'])
    expect_fail('A17 close with a fake creator account', [close_ix(eve.pubkey(), M, eve.pubkey())], eve, [eve], ERR['BadAccount'])
    b = lamports(human.pubkey()); send([close_ix(eve.pubkey(), M, human.pubkey())], eve, [eve])
    print(f"  pass  A18 close by a stranger refunds the CREATOR: +{(lamports(human.pubkey()) - b)/1e9:.4f} SOL, match account gone: {read_match(M) is None}")


S1 = [(2, 'alice', 'buy', 2.0), (4, 'bob', 'buy', 1.5), (6, 'carol', 'buy', 3.0), (10, 'dave', 'buy', 1.0), (15, 'alice', 'sell', 1.0),
      (20, 'erin', 'buy', 2.0), (25, 'bob', 'sell', 1.0), (30, 'carol', 'sell', 0.5), (40, 'dave', 'sell', 1.0), (45, 'frank', 'buy', 0.5)]
S2 = [(2, 'alice', 'buy', 3.0), (6, 'whale', 'fill', 0), (20, 'bob', 'buy', 2.0), (26, 'alice', 'sell', 1.0), (32, 'carol', 'buy', 5.0),
      (42, 'bob', 'sell', 1.0), (52, 'carol', 'sell', 0.5), (58, 'whale', 'sell', 0.25)]
S3 = [(2, 'alice', 'buy', 1.0), (10, 'alice', 'sell', 1.0)]
which = sys.argv[1:] or (['S1', 'S2', 'S3', 'ATTACKS'] if __name__ == '__main__' else [])
if 'ATTACKS' in which: attacks()
if 'S1' in which: scenario('S1 curve coin', S1, lock=60, lo=50, hi=60)
if 'S2' in which: scenario('S2 graduating coin', S2, lock=75, lo=64, hi=75)
if 'S3' in which: scenario('S3 dead scorer', S3, lock=30, lo=20, hi=30, timeout=25, scorer_on=False)
json.dump(RESULTS, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pino_test.json'), 'w'), indent=1, default=str)
bad = [r for r in RESULTS if r.get('ok') is False]
print(f"\n{len([r for r in RESULTS if 'attack' in r])} attack checks, {len(bad)} failed" if any('attack' in r for r in RESULTS) else '')
