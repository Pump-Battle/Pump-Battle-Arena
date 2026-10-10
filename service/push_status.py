"""Build the 'matches/current' record for the website from the keeper's state + the chain.

    python push_status.py <mint> <name> <symbol> [--board]      -> writes ~/.local/state/arena/current.json

--board recomputes the leaderboard from the chain (slow-ish, ~20 s); without it the previous board is kept.
"""
import os, sys, json, time
os.environ.setdefault('ARENA_LIB', 'pinolib')
for line in open(os.path.expanduser('~/.config/arena/keeper.env')):
    k, _, v = line.strip().partition('='); os.environ.setdefault(k, v)
os.environ['FIGHT_RPC'] = os.environ['ARENA_RPC']; os.environ['FIGHT_CU_PRICE'] = os.environ.get('ARENA_CU_PRICE', '200000')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'client'))
from keeper import *
mint, name, symbol = sys.argv[1], sys.argv[2], sys.argv[3]
ST = os.path.expanduser('~/.local/state/arena'); out_p = f'{ST}/current.json'
prev = json.load(open(out_p)) if os.path.exists(out_p) and json.load(open(out_p)).get('mint') == mint else {}
M = P(mint); m = read_match(M)
ev = [json.loads(l) for l in open(f'{ST}/keeper.jsonl') if mint in l]
t_of = lambda name: next((e['t'] for e in ev if e['ev'] == name), None)
launch = next((json.loads(l) for f in [f'{ST}/canary_{mint}.jsonl'] if os.path.exists(f) for l in open(f) if '"launched"' in l), {})
rec = dict(mint=mint, name=name, symbol=symbol, headline='Live match · blitz tier, 5 minutes', t_launch=launch.get('t'),
           program='36ZCF6tSXPc8FpG9wYsVZrWnPNtqUAd7b619MZ8MqKtR', updated=int(time.time()))
if m is None:   # closed
    rec.update(phase='closed', status='Closed', t_unlock=prev.get('t_unlock'), t_bell=prev.get('t_bell') or t_of('candle'), t_paid=t_of('claimed') or t_of('expired'),
               pot=prev.get('pot', 0), fees=prev.get('fees', 0), cost=prev.get('cost', 0), proceeds=prev.get('proceeds', 0), board=prev.get('board', []), hash=prev.get('hash'),
               pot_sub=prev.get('pot_sub', ''), clock_sub='match over. Rent refunded to the creator.', traders=prev.get('traders'))
else:
    rec.update(t_unlock=m['unlock_ts'], t_bell=m['candle_end'] or None, t_deadline=m['deadline'] or None, cost=m['cost_basis'] / 1e9, proceeds=m['proceeds'] / 1e9,
               fees=m['fee_total'] / 1e9, pot=(m['prize_pot'] or prev.get('pot_paid', 0)) / 1e9 if not m['claimed'] else prev.get('pot', 0), hash=m['results_hash'] if isinstance(m['results_hash'], str) else m['results_hash'].hex())
    if m['claimed']: rec.update(phase='paid', status='Paid out', t_paid=t_of('claimed'), pot_sub='paid to the winner')
    elif m['expired']: rec.update(phase='expired', status='Expired', t_paid=t_of('expired'), pot_sub='nobody qualified: pot went to the fallback')
    elif m['deadline']: rec.update(phase='posted', status='Result posted', pot_sub='in the pot. Paid after the challenge window.')
    elif m['candle_end']: rec.update(phase='bell', status='Bell rung', pot_sub='in the pot. Scoring from the chain…')
    elif m['sold']: rec.update(phase='sold', status='Bag sold', pot_sub='in the pot')
    else: rec.update(phase='launched', status='Live · locked', pot_sub='in the pot so far, from creator fees')
    rec['board'] = prev.get('board', [])
    if '--board' in sys.argv:
        until = m['candle_end'] or int(time.time()); excl = {str(vault_pda(M, m['human'])), str(m['keeper'])}
        board, h, _ = score(M, until, excl)
        rec['board'] = [dict(w=w, realized=b['realized'] / 1e9, sells=b['sells']) for w, b in board[:10]]
        # every sell by a ranked wallet, in order: the hits the fight animation plays
        ranked = {w for w, _ in board}; book = {}; hits = []
        for bt, sig, w, dl, dt in chain_trades(M, until, excl):
            b = book.setdefault(w, dict(cost=0.0, tok=0))
            if dt > 0: b['cost'] += -dl; b['tok'] += dt
            elif b['tok'] > 0:
                q = min(-dt, b['tok']); basis = b['cost'] * q / b['tok']; b['cost'] -= basis; b['tok'] -= q
                if w in ranked: hits.append(dict(t=bt - (launch.get('t') or bt), w=w, d=round((dl - basis) / 1e9, 6)))
        rec['hits'] = hits[-60:]
    if '--board' in sys.argv:
        sigs = rpc.call('getSignaturesForAddress', [str(pf.bonding_curve(M)), {'limit': 1000}])
        rec['traders'] = max(prev.get('traders') or 0, len({s['signature'] for s in sigs}) and None or 0) or prev.get('traders')
    rec['rank_note'] = f"Ranked on the profit each wallet actually took. {len(rec['board'])} wallet{'s' if len(rec['board']) != 1 else ''} ranked so far."
json.dump(rec, open(out_p, 'w'), indent=1); json.dump(rec, open(f'{ST}/current_{mint}.json', 'w'))
print(json.dumps({k: v for k, v in rec.items() if k != 'board'}, default=str)); print('board:', [(b['w'][:8], round(b['realized'], 4)) for b in rec['board']])
