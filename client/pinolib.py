"""Client for the Pinocchio arena contract (36ZCF6tS...). Same function names as hybridlib, so keeper.py runs it
with ARENA_LIB=pinolib. Instructions are a 1-byte tag; the match account is a raw 376-byte layout."""
import struct
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from chainlib import *

PROG = P('36ZCF6tSXPc8FpG9wYsVZrWnPNtqUAd7b619MZ8MqKtR')
HYB = PROG
SLOT_HASHES = P('SysvarS1otHashes111111111111111111111111111')
_HUMAN = {}


def match_pda(mint, with_bump=False):
    a, b = Pubkey.find_program_address([b'match', bytes(mint)], PROG)
    return (a, b) if with_bump else a


def vault_pda(mint, human=None, with_bump=False):
    if human is None:
        human = _HUMAN.get(str(mint)) or read_match(mint)['human']
    a, b = Pubkey.find_program_address([b'vault', bytes(mint), bytes(human)], PROG)
    return (a, b) if with_bump else a


def _rem(ix, vault):
    return [AccountMeta(a.pubkey, False if a.pubkey == vault else a.is_signer, a.is_writable) for a in ix.accounts]


def launch_ixs(human, mint, name, symbol, dev_sol, lock, lo, hi, challenge, timeout, gain_bps, fee_bps, scorer, fallback,
               keeper=None, grace=30, buy_override=None):
    keeper = keeper or human
    _HUMAN[str(mint)] = human
    m, mb = match_pda(mint, True); v, vb = vault_pda(mint, human, True)
    c = create_v2_ix(mint, human, name, symbol)
    data = bytearray(c.data); off = 8
    for _ in range(3):
        off += 4 + struct.unpack_from('<I', data, off)[0]
    data[off:off + 32] = bytes(v)
    create = Instruction(c.program_id, bytes(data), c.accounts)
    lam = int(dev_sol * LAMPORTS)
    fresh = pf.Curve(30.0, 1.073e9, 0, 793.1e6, None, False, v)
    amount, _ = pf.buy_quote(fresh, lam, slippage_bps=0, fee_bps=125)
    amount = amount * 999 // 1000
    graw = global_raw(); fr = pf.fee_recipient_for(fresh, graw)[0]; bb = pf.buyback_fee_recipients(graw)[0]
    buy = buy_override or pf.buy_v2_ix(v, mint, v, TOKEN22, fr, bb, amount, lam)
    body = (bytes([0, mb, vb]) + struct.pack('<qqqqqq', lock, lo, hi, challenge, timeout, grace) + struct.pack('<HH', gain_bps, fee_bps)
            + bytes(scorer) + bytes(keeper) + bytes(fallback) + struct.pack('<Q', lam) + bytes(buy.data))
    metas = [AccountMeta(human, True, True), AccountMeta(mint, False, False), AccountMeta(pf.bonding_curve(mint), False, False),
             AccountMeta(m, False, True), AccountMeta(v, False, True), AccountMeta(pf.ata(v, mint, TOKEN22), False, True),
             AccountMeta(pf.PUMP, False, False), AccountMeta(SYSTEM, False, False)]
    return [create, pf.create_ata_idempotent_ix(human, v, mint, TOKEN22), Instruction(PROG, body, metas + _rem(buy, v))]


def _collect_ix(mint, v, venue):
    if venue == 1:
        return Instruction(pf.PUMP, disc('collect_creator_fee'), [AccountMeta(v, False, True), AccountMeta(pf.creator_vault(v), False, True),
               AccountMeta(SYSTEM, False, False), AccountMeta(pf.event_authority(), False, False), AccountMeta(pf.PUMP, False, False)])
    ca = pf.amm_creator_vault_authority(v)
    return Instruction(pf.AMM, disc('collect_coin_creator_fee'), [
        AccountMeta(WSOL, False, False), AccountMeta(TOKEN, False, False), AccountMeta(v, False, False), AccountMeta(ca, False, False),
        AccountMeta(pf.ata(ca, WSOL, TOKEN), False, True), AccountMeta(pf.ata(v, WSOL, TOKEN), False, True),
        AccountMeta(pf.amm_event_authority(), False, False), AccountMeta(pf.AMM, False, False)])


def sweep_ixs(caller, mint, human, venue):
    v = vault_pda(mint, human); wsol = pf.ata(v, WSOL, TOKEN)
    col = _collect_ix(mint, v, venue) if venue else None
    target = pf.AMM if venue == 2 else pf.PUMP
    cd = bytes(col.data) if col else b''
    metas = [AccountMeta(caller, True, True), AccountMeta(match_pda(mint), False, True), AccountMeta(human, False, True),
             AccountMeta(v, False, True), AccountMeta(wsol, False, True), AccountMeta(target, False, False),
             AccountMeta(TOKEN, False, False), AccountMeta(SYSTEM, False, False)]
    ix = Instruction(PROG, bytes([1, venue]) + struct.pack('<H', len(cd)) + cd, metas + (_rem(col, v) if col else []))
    return ([pf.create_ata_idempotent_ix(caller, v, WSOL, TOKEN)] if venue == 2 else []) + [ix]


def sell_ix_for(mint, v, tokens, min_out=0):
    c = curve(mint)
    if c is not None and not c.complete:
        fr, bb = fee_accounts(mint)
        return pf.sell_v2_ix(v, mint, v, TOKEN22, fr, bb, tokens, min_out), pf.PUMP
    pool = amm_pool(mint); fr, bb = amm_fee_accounts()
    return pf.amm_sell_ix(v, mint, pool, TOKEN22, fr, bb, tokens, min_out), pf.AMM


def settle_ixs(caller, mint, human, tokens, min_out=0):
    v = vault_pda(mint, human); wsol = pf.ata(v, WSOL, TOKEN)
    sell, target = sell_ix_for(mint, v, tokens, min_out)
    metas = [AccountMeta(caller, True, True), AccountMeta(match_pda(mint), False, True), AccountMeta(human, False, True),
             AccountMeta(v, False, True), AccountMeta(pf.ata(v, mint, TOKEN22), False, True), AccountMeta(wsol, False, True),
             AccountMeta(target, False, False), AccountMeta(TOKEN, False, False), AccountMeta(SYSTEM, False, False)]
    pre = [pf.create_ata_idempotent_ix(caller, v, WSOL, TOKEN)] if target == pf.AMM else []
    return pre + [Instruction(PROG, bytes([2]) + struct.pack('<H', len(sell.data)) + bytes(sell.data), metas + _rem(sell, v))]


def draw_ix(caller, mint):
    return Instruction(PROG, bytes([3]), [AccountMeta(caller, True, False), AccountMeta(match_pda(mint), False, True), AccountMeta(SLOT_HASHES, False, False)])


def post_ix(scorer, mint, winner, score, h):
    return Instruction(PROG, bytes([4]) + bytes(winner) + struct.pack('<q', score) + h,
                       [AccountMeta(scorer, True, False), AccountMeta(match_pda(mint), False, True)])


def pay_ix(name, caller, mint, recipient):
    return Instruction(PROG, bytes([5 if name == 'claim' else 6]), [AccountMeta(caller, True, False), AccountMeta(match_pda(mint), False, True),
                       AccountMeta(vault_pda(mint), False, True), AccountMeta(recipient, False, True), AccountMeta(SYSTEM, False, False)])


def close_ix(caller, mint, human):
    return Instruction(PROG, bytes([7]), [AccountMeta(caller, True, False), AccountMeta(human, False, True), AccountMeta(match_pda(mint), False, True)])


def sweep_closed_ixs(caller, mint, human, venue):
    v, vb = vault_pda(mint, human, True); wsol = pf.ata(v, WSOL, TOKEN)
    col = _collect_ix(mint, v, venue)
    metas = [AccountMeta(caller, True, True), AccountMeta(human, False, True), AccountMeta(v, False, True), AccountMeta(mint, False, False),
             AccountMeta(match_pda(mint), False, False), AccountMeta(wsol, False, True), AccountMeta(pf.AMM if venue == 2 else pf.PUMP, False, False),
             AccountMeta(TOKEN, False, False), AccountMeta(SYSTEM, False, False)]
    mb = match_pda(mint, True)[1]
    ix = Instruction(PROG, bytes([8, vb, venue]) + struct.pack('<H', len(col.data)) + bytes([mb]) + bytes(col.data), metas + _rem(col, v))
    return ([pf.create_ata_idempotent_ix(caller, v, WSOL, TOKEN)] if venue == 2 else []) + [ix]


LAYOUT = dict(human=(8, 'pk'), mint=(40, 'pk'), scorer=(72, 'pk'), keeper=(104, 'pk'), fallback=(136, 'pk'), winner=(168, 'pk'),
              results_hash=(200, '32s'), start_ts=(232, 'q'), unlock_ts=(240, 'q'), window_lo=(248, 'q'), window_hi=(256, 'q'),
              challenge_secs=(264, 'q'), result_timeout=(272, 'q'), keeper_grace=(280, 'q'), candle_end=(288, 'q'), deadline=(296, 'q'),
              score=(304, 'q'), cost_basis=(312, 'Q'), tokens_locked=(320, 'Q'), proceeds=(328, 'Q'), gain_prize=(336, 'Q'),
              fee_total=(344, 'Q'), fee_prize=(352, 'Q'), prize_pot=(360, 'Q'), paid_creator=(368, 'Q'), gain_bps=(4, 'H'), fee_bps=(6, 'H'))


def read_match(mint):
    raw = raw_account(match_pda(mint))
    if raw is None or len(raw) < 376:
        return None
    out = {k: (Pubkey(raw[o:o + 32]) if t == 'pk' else struct.unpack_from('<' + t, raw, o)[0]) for k, (o, t) in LAYOUT.items()}
    f = raw[2]; out.update(sold=bool(f & 1), claimed=bool(f & 2), expired=bool(f & 4))
    _HUMAN[str(mint)] = out['human']
    return out


def chain_time():
    return rpc.call('getBlockTime', [rpc.call('getSlot', [{'commitment': 'confirmed'}])])
