"""The automation: one keeper loop runs a hybrid match from launch to payout with no human.

Each tick it reads the match account and does whatever is due, in this order:
  sweep fees (curve, and PumpSwap once graduated)    every `sweep_every` seconds
  graduate the coin (migrate_v2) if the curve is full  permissionless
  sell the bag                                         once the lock ends
  draw the candle end                                  once the lock ends
  score from the chain and post the winner             scorer key
  claim the prize to the winner                        after the challenge window
  expire the prize to the fallback                     if no result in time
  keep sweeping fees to the creator                    forever after the payout

The scorer reads ONLY public chain data: every transaction touching the coin's bonding curve and
pool, the fee payer's SOL change and token change in each, and the block time. Anyone can rerun it
and compare against the hash the contract stores.
"""
import json, time, hashlib, collections
import os, importlib
_lib = importlib.import_module(os.environ.get('ARENA_LIB', 'hybridlib'))   # hybridlib (Anchor) or pinolib (Pinocchio)
globals().update({k: getattr(_lib, k) for k in dir(_lib) if not k.startswith('_')})

EXCLUDE_NOTE = 'vault (the creator\'s locked bag), the keeper, wallets that received tokens without trading'


def chain_trades(mint, until_ts, exclude):
    """Every trade on the coin from chain history alone: (block_time, wallet, d_lamports, d_tokens)."""
    v = vault_pda(mint)
    addrs = [pf.bonding_curve(mint)]
    pool = amm_pool(mint)
    if pool is not None:
        addrs.append(pool.pool)
    sigs = {}
    for a in addrs:
        before = None
        while True:
            opts = {'limit': 1000, 'commitment': 'confirmed'}
            if before: opts['before'] = before
            page = rpc.call('getSignaturesForAddress', [str(a), opts])
            for s in page:
                if s.get('err') is None and s.get('blockTime') and s['blockTime'] <= until_ts:
                    sigs[s['signature']] = s['blockTime']
            if len(page) < 1000: break
            before = page[-1]['signature']
    out = []
    for sig, bt in sigs.items():
        tx = rpc.call('getTransaction', [sig, {'encoding': 'json', 'commitment': 'confirmed', 'maxSupportedTransactionVersion': 0}])
        keys = tx['transaction']['message']['accountKeys']; m = tx['meta']
        payer = keys[0]
        if payer in exclude:
            continue
        progs = {keys[ix['programIdIndex']] for ix in tx['transaction']['message']['instructions']}
        if not progs & {str(pf.PUMP), str(pf.AMM)}:
            continue
        tok = lambda bals: sum(int(b['uiTokenAmount']['amount']) for b in (bals or []) if b.get('owner') == payer and b['mint'] == str(mint))
        dt = tok(m.get('postTokenBalances')) - tok(m.get('preTokenBalances'))
        if dt == 0:
            continue
        out.append((bt, sig, payer, m['postBalances'][0] - m['preBalances'][0], dt))
    return sorted(out)


def score(mint, until_ts, exclude):
    book = collections.defaultdict(lambda: dict(cost=0.0, tok=0, realized=0.0, sells=0, last=0))
    for bt, sig, w, dl, dt in chain_trades(mint, until_ts, exclude):
        b = book[w]
        if dt > 0:
            b['cost'] += -dl; b['tok'] += dt
        elif b['tok'] > 0:
            q = min(-dt, b['tok']); basis = b['cost'] * q / b['tok']
            b['realized'] += dl - basis; b['cost'] -= basis; b['tok'] -= q; b['sells'] += 1
        b['last'] = bt
    board = sorted([(w, b) for w, b in book.items() if b['sells'] > 0], key=lambda kv: (-kv[1]['realized'], kv[1]['last']))
    blob = json.dumps([[w, int(b['realized'])] for w, b in board], separators=(',', ':'))
    return board, hashlib.sha256(blob.encode()).digest(), blob


class Keeper:
    """One tick = read the match and do whatever is due. Safe to call repeatedly; a failed step is
    logged and retried on the next tick, so nothing needs a human to restart it."""

    def __init__(self, mint, keeper, scorer, log, sweep_every=15):
        self.mint, self.k, self.scorer, self.log, self.sweep_every = mint, keeper, scorer, log, sweep_every
        self.last_sweep, self.done = 0, False

    def _send(self, what, ixs, signer, cu=400_000):
        try:
            send(ixs, signer, [signer], cu=cu); return True
        except Exception as e:
            self.log('error', step=what, err=str(e).split(chr(10))[0][:160]); return False

    def tick(self):
        mint, k = self.mint, self.k
        m = read_match(mint); now = chain_time(); h = m['human']
        c = curve(mint)
        if c is not None and c.complete and amm_pool(mint) is None:
            if self._send('graduate', [graduate_ix(k.pubkey(), mint)], k, cu=1_000_000): self.log('graduated')
        graduated = amm_pool(mint) is not None
        closed = m['claimed'] or m['expired']
        if now - self.last_sweep >= self.sweep_every or closed:
            amm_fee_acct = pf.ata(pf.amm_creator_vault_authority(vault_pda(mint)), WSOL, TOKEN)
            amm_ready = graduated and raw_account(amm_fee_acct) is not None     # exists after the first PumpSwap trade
            for venue in ([1, 2] if amm_ready else [1]):
                pre = read_match(mint)
                self._send('sweep', sweep_ixs(k.pubkey(), mint, h, venue), k)
                post = read_match(mint)
                if post['fee_total'] > pre['fee_total']:
                    self.log('sweep', venue='PumpSwap' if venue == 2 else 'curve', fees=(post['fee_total'] - pre['fee_total']) / 1e9,
                             to_prize=(post['fee_prize'] - pre['fee_prize']) / 1e9, to_creator=(post['paid_creator'] - pre['paid_creator']) / 1e9,
                             pot=post['prize_pot'] / 1e9)
            self.last_sweep = now
            if closed:
                if 'keeper_grace' in m and read_match(mint)['prize_pot'] == 0:
                    if self._send('close', [close_ix(k.pubkey(), mint, h)], k): self.log('closed', rent_back_to_creator=True)
                self.done = True; return
        if now >= m['unlock_ts'] and not m['sold']:
            floor = self.floor(mint, m['tokens_locked']) if hasattr(self, 'floor') else 0
            ixs = settle_ixs(k.pubkey(), mint, h, m['tokens_locked'], floor) if 'keeper_grace' in m else settle_ixs(k.pubkey(), mint, h, m['tokens_locked'])
            if self._send('sell', ixs, k, cu=700_000):
                s = read_match(mint)
                self.log('sold', venue='PumpSwap' if graduated else 'curve', proceeds=s['proceeds'] / 1e9, cost_basis=s['cost_basis'] / 1e9,
                         gain_prize=s['gain_prize'] / 1e9, pot=s['prize_pot'] / 1e9)
            return
        if now >= m['unlock_ts'] and m['candle_end'] == 0:
            if self._send('draw', [draw_ix(k.pubkey(), mint)], k):
                d = read_match(mint); self.log('candle', end_offset_s=d['candle_end'] - d['start_ts'])
            return
        if self.scorer and m['sold'] and m['candle_end'] and m['deadline'] == 0 and not m['expired']:
            board, hsh, _ = score(mint, m['candle_end'], {str(vault_pda(mint)), str(k.pubkey())})
            if board:
                w, b = board[0]
                if self._send('post', [post_ix(self.scorer.pubkey(), mint, P(w), int(b['realized']), hsh)], self.scorer):
                    self.log('posted', winner=w, realized=b['realized'] / 1e9, entrants=len(board), leaderboard_hash=hsh.hex()[:16])
            return
        if m['deadline'] and now >= m['deadline'] and not m['claimed']:
            if self._send('claim', [pay_ix('claim', k.pubkey(), mint, m['winner'])], k):
                self.log('claimed', winner=str(m['winner']), amount=m['prize_pot'] / 1e9)
            return
        if not closed and m['deadline'] == 0 and m['candle_end'] and now >= m['unlock_ts'] + m['result_timeout']:
            if self._send('expire', [pay_ix('expire', k.pubkey(), mint, m['fallback'])], k):
                self.log('expired', fallback=str(m['fallback']), amount=m['prize_pot'] / 1e9)


def _floor(self, mint, tokens, keep=0.97):
    """Keeper's price floor for the sale: 97% of what the bag fetches at the reserves right now. Anyone who
    pushes the price down first (a sandwich) makes the sale FAIL instead of fill cheap; the keeper retries."""
    c = curve(mint)
    if c is not None and not c.complete:
        out = c.v_sol * 1e9 - (c.v_sol * 1e9 * c.v_tok * 1e6) / (c.v_tok * 1e6 + tokens)
        return int(out * (1 - 0.0125) * keep)
    qs, qt = pool_reserves(mint)
    return int((qs - qs * qt / (qt + tokens)) * 0.97 * keep)


Keeper.floor = _floor
