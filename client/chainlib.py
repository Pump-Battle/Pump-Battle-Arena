"""Sim-chain helpers for the Battle Bot Arena replay.

Talks to the SECOND test validator on vote (192.168.2.123:18899), which runs the
real pump.fun / PumpSwap / fee / mayhem programs cloned from mainnet. Buy and sell
instructions come from the frozen copy of the mainnet-validated client
(pumpfun_frozen.py, a copy of /mnt/ssd2/swarm-trade/pumpfun.py -- never import the
live one). create_v2 and migrate are encoded here from pump.fun's on-chain IDL.
"""
import base64, hashlib, json, struct, time
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.message import Message
from solders.transaction import Transaction
from solders.hash import Hash
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
import pumpfun_frozen as pf

import os
RPC_URL = os.environ.get('FIGHT_RPC', 'http://192.168.2.123:18899')   # the sim chain unless told otherwise
CU_PRICE = int(os.environ.get('FIGHT_CU_PRICE', '0'))                   # micro-lamports per CU (mainnet priority fee)
P = Pubkey.from_string
SYSTEM = P('11111111111111111111111111111111')
TOKEN = P('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA')
TOKEN22 = P('TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb')
ATA_PROG = P('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL')
MAYHEM = P('MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e')
RENT = P('SysvarRent111111111111111111111111111111111')
LAMPORTS = 1_000_000_000

rpc = pf.Rpc(RPC_URL)


def disc(name):
    return hashlib.sha256(f'global:{name}'.encode()).digest()[:8]


def borsh_str(s):
    b = s.encode()
    return struct.pack('<I', len(b)) + b


def raw_account(addr):
    v = rpc.call('getAccountInfo', [str(addr), {'encoding': 'base64', 'commitment': 'confirmed'}])['value']
    return None if v is None else base64.b64decode(v['data'][0])


def lamports(addr):
    return rpc.call('getBalance', [str(addr), {'commitment': 'confirmed'}])['value']


def token_balance(owner, mint, token_program=TOKEN22):
    a = pf.ata(owner, mint, token_program)
    v = rpc.call('getAccountInfo', [str(a), {'encoding': 'base64', 'commitment': 'confirmed'}])['value']
    if v is None:
        return 0
    return int.from_bytes(base64.b64decode(v['data'][0])[64:72], 'little')


def airdrop(pub, sol):
    sig = rpc.call('requestAirdrop', [str(pub), int(sol * LAMPORTS), {'commitment': 'confirmed'}])
    confirm(sig)
    return sig


def confirm(sig, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = rpc.call('getSignatureStatuses', [[str(sig)]])['value'][0]
        if st and st.get('confirmationStatus') in ('confirmed', 'finalized'):
            if st.get('err'):
                raise RuntimeError(f'tx {sig} failed: {st["err"]}')
            return st
        time.sleep(0.5 if CU_PRICE else 0.05)      # mainnet: 2 polls/s, well inside a shared key's rate limit
    raise TimeoutError(f'tx {sig} not confirmed in {timeout}s')


def send(ixs, payer, signers, cu=400_000, simulate_first=True, wait=True):
    bh = rpc.call('getLatestBlockhash', [{'commitment': 'confirmed'}])['value']['blockhash']
    budget = [set_compute_unit_limit(cu)]  # (unchanged) + ([set_compute_unit_price(CU_PRICE)] if CU_PRICE else [])
    msg = Message.new_with_blockhash(budget + list(ixs), payer.pubkey(), Hash.from_string(bh))
    tx = Transaction(list(signers), msg, Hash.from_string(bh))
    b64 = base64.b64encode(bytes(tx)).decode()
    if simulate_first:
        sim = rpc.call('simulateTransaction', [b64, {'encoding': 'base64', 'commitment': 'confirmed'}])['value']
        for _ in range(3):          # a load-balanced RPC may not know a fresh blockhash yet: rebuild on a newer one
            if sim.get('err') != 'BlockhashNotFound': break
            time.sleep(1.5)
            bh = rpc.call('getLatestBlockhash', [{'commitment': 'confirmed'}])['value']['blockhash']
            msg = Message.new_with_blockhash(budget + list(ixs), payer.pubkey(), Hash.from_string(bh))
            tx = Transaction(list(signers), msg, Hash.from_string(bh)); b64 = base64.b64encode(bytes(tx)).decode()
            sim = rpc.call('simulateTransaction', [b64, {'encoding': 'base64', 'commitment': 'confirmed'}])['value']
        if sim.get('err'):
            logs = '\n    '.join((sim.get('logs') or [])[-25:])
            raise RuntimeError(f'simulation failed: {sim["err"]}\n    {logs}')
    sig = rpc.call('sendTransaction', [b64, {'encoding': 'base64', 'skipPreflight': True}])
    if wait:
        if CU_PRICE:                     # REBROADCAST on mainnet: resend the same signed tx until it lands
            t0 = time.time()
            while True:
                try:
                    confirm(sig, timeout=3); break
                except TimeoutError:
                    if time.time() - t0 > 80: raise
                    rpc.call('sendTransaction', [b64, {'encoding': 'base64', 'skipPreflight': True, 'maxRetries': 0}])
        else:
            confirm(sig)
    return sig


def tx_meta(sig):
    """Return (fee_lamports, pre/post balances keyed by pubkey) for P&L cross-checks."""
    r = rpc.call('getTransaction', [str(sig), {'encoding': 'json', 'commitment': 'confirmed', 'maxSupportedTransactionVersion': 0}])
    keys = r['transaction']['message']['accountKeys']
    m = r['meta']
    return m['fee'], {k: (a, b) for k, a, b in zip(keys, m['preBalances'], m['postBalances'])}


# ------------------------------------------------------------------ pump.fun
def global_raw():
    return raw_account(pf.global_pda())


_FEE_CACHE = {}


def fee_accounts(mint=None, graw=None):
    """(fee_recipient, buyback) for THIS coin: the accepted recipient depends on its cashback flag.
    Cached per mint: the flag is fixed at creation and the global recipients don't move during a sim."""
    if mint is not None and mint in _FEE_CACHE:
        return _FEE_CACHE[mint]
    graw = graw or global_raw()
    c = curve(mint) if mint is not None else None
    fr = pf.fee_recipient_for(c, graw)[0] if c is not None else pf.authorized_fee_recipient(graw)
    out = (fr, pf.buyback_fee_recipients(graw)[0])
    if mint is not None and c is not None:
        _FEE_CACHE[mint] = out
    return out


def mayhem_state(mint):
    return Pubkey.find_program_address([b'mayhem-state', bytes(mint)], MAYHEM)[0]


def create_v2_ix(mint, creator, name, symbol, uri='https://example.invalid/fight.json'):
    bc = pf.bonding_curve(mint)
    sol_vault = P('BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s')
    data = (disc('create_v2') + borsh_str(name) + borsh_str(symbol) + borsh_str(uri) + bytes(creator)
            + bytes([0])            # is_mayhem_mode = false
            + bytes([0])            # is_cashback_enabled  (OptionBool = struct{bool})
            + struct.pack('<Q', 0)  # creator_fee_bps      (OptionU64  = struct{u64})
            + bytes([0]))           # is_holder_reward     (OptionBool)
    metas = [AccountMeta(mint, True, True),
             AccountMeta(P('TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM'), False, False),
             AccountMeta(bc, False, True),
             AccountMeta(pf.ata(bc, mint, TOKEN22), False, True),
             AccountMeta(pf.global_pda(), False, False),
             AccountMeta(creator, True, True),
             AccountMeta(SYSTEM, False, False),
             AccountMeta(TOKEN22, False, False),
             AccountMeta(ATA_PROG, False, False),
             AccountMeta(MAYHEM, False, True),
             AccountMeta(P('13ec7XdrjF3h3YcqBTFDSReRcUFwbCnJaAQspM4j6DDJ'), False, False),
             AccountMeta(sol_vault, False, True),
             AccountMeta(mayhem_state(mint), False, True),
             AccountMeta(pf.ata(sol_vault, mint, TOKEN22), False, True),
             AccountMeta(pf.event_authority(), False, False),
             AccountMeta(pf.PUMP, False, False)]
    return Instruction(pf.PUMP, data, metas)


def curve(mint):
    raw = raw_account(pf.bonding_curve(mint))
    return None if raw is None else pf.decode_curve(raw)


def buy_sol_ix(user, mint, creator, sol, slippage_bps=300):
    """Spend about `sol` SOL on the curve: tokens out quoted from the live virtual reserves."""
    c = curve(mint)
    amount, max_cost = pf.buy_quote(c, int(sol * LAMPORTS), slippage_bps=slippage_bps, fee_bps=125)
    fr, bb = fee_accounts(mint)
    return pf.buy_v2_ix(user, mint, creator, TOKEN22, fr, bb, amount, max_cost), amount


def sell_tokens_ix(user, mint, creator, amount):
    fr, bb = fee_accounts(mint)
    return pf.sell_v2_ix(user, mint, creator, TOKEN22, fr, bb, amount, 0)


# ------------------------------------------------------------------ graduation + PumpSwap
WSOL = pf.WSOL


def migrate_ix(user, mint):
    """pump.fun `migrate` -- permissionless: withdraw_authority is checked against global, not a signer."""
    graw = global_raw()
    withdraw_authority = Pubkey(graw[113:145])
    bc = pf.bonding_curve(mint)
    pa = pf.amm_pool_authority(mint)
    pool = pf.amm_pool(mint)
    lp_mint = Pubkey.find_program_address([b'pool_lp_mint', bytes(pool)], pf.AMM)[0]
    acc = dict(global_=pf.global_pda(), withdraw_authority=withdraw_authority, mint=mint, bonding_curve=bc,
               associated_bonding_curve=pf.ata(bc, mint, TOKEN22), user=user, system_program=SYSTEM,
               token_program=TOKEN, pump_amm=pf.AMM, pool=pool, pool_authority=pa,
               pool_authority_mint_account=pf.ata(pa, mint, TOKEN22), pool_authority_wsol_account=pf.ata(pa, WSOL, TOKEN),
               amm_global_config=pf.amm_global_config(), wsol_mint=WSOL, lp_mint=lp_mint,
               user_pool_token_account=pf.ata(pa, lp_mint, TOKEN22), pool_base_token_account=pf.ata(pool, mint, TOKEN22),
               pool_quote_token_account=pf.ata(pool, WSOL, TOKEN), token_2022_program=TOKEN22,
               associated_token_program=ATA_PROG, pump_amm_event_authority=pf.amm_event_authority(),
               event_authority=pf.event_authority(), program=pf.PUMP, rent=RENT)
    idl = json.load(open('/mnt/ssd2/fight_sim/chain/pump_idl.json'))
    spec = next(i for i in idl['instructions'] if i['name'] == 'migrate')
    metas = [AccountMeta(acc['global_' if a['name'] == 'global' else a['name']], bool(a.get('signer')), bool(a.get('writable')))
             for a in spec['accounts']]
    return Instruction(pf.PUMP, bytes(spec['discriminator']), metas)


def amm_pool(mint):
    return pf.amm_pool_state(rpc, mint)


_AMM_FEE = []


def amm_fee_accounts():
    if _AMM_FEE:
        return _AMM_FEE[0]
    craw = raw_account(pf.amm_global_config())
    prot, buyb = pf.amm_fee_recipients(craw)
    _AMM_FEE.append((prot[0], buyb[0]))
    return prot[0], buyb[0]


def pool_reserves(mint, pool=None):
    pool = pool or amm_pool(mint)
    return pf._ta_amount(rpc, pool.quote_ta), pf._ta_amount(rpc, pool.base_ta)


def amm_buy_ixs(user, mint, sol, slippage_bps=300):
    pool = amm_pool(mint)
    lam = int(sol * LAMPORTS)
    q = pf.amm_buy_quote(rpc, pool, lam, slippage_bps=slippage_bps, fee_bps=150)
    if q is None:
        raise RuntimeError('pool has no reserves')
    base_out, max_in = q
    fr, bb = amm_fee_accounts()
    return (pf.wrap_sol_ixs(user, lam)
            + [pf.create_ata_idempotent_ix(user, user, mint, TOKEN22),
               pf.amm_buy_ix(user, mint, pool, TOKEN22, fr, bb, base_out, max_in),
               pf.close_ata_ix(user, pf.ata(user, WSOL, TOKEN), TOKEN)]), base_out


def amm_sell_ixs(user, mint, amount):
    pool = amm_pool(mint)
    fr, bb = amm_fee_accounts()
    return [pf.create_ata_idempotent_ix(user, user, WSOL, TOKEN),
            pf.amm_sell_ix(user, mint, pool, TOKEN22, fr, bb, amount, 0),
            pf.close_ata_ix(user, pf.ata(user, WSOL, TOKEN), TOKEN)]


def migrate_v2_ix(user, mint, remaining=()):
    """pump.fun migrate_v2 (what mainnet uses). Permissionless; 2 undocumented trailing accounts."""
    graw = global_raw()
    bc = pf.bonding_curve(mint); pa = pf.amm_pool_authority(mint); pool = pf.amm_pool(mint)
    lp_mint = Pubkey.find_program_address([b'pool_lp_mint', bytes(pool)], pf.AMM)[0]
    acc = dict(global_=pf.global_pda(), withdraw_authority=Pubkey(graw[113:145]), base_mint=mint, quote_mint=WSOL,
               bonding_curve=bc, associated_base_bonding_curve=pf.ata(bc, mint, TOKEN22),
               associated_quote_bonding_curve=pf.ata(bc, WSOL, TOKEN), user=user, system_program=SYSTEM,
               pump_amm=pf.AMM, pool=pool, pool_authority=pa, pool_authority_mint_account=pf.ata(pa, mint, TOKEN22),
               pool_authority_quote_account=pf.ata(pa, WSOL, TOKEN), amm_global_config=pf.amm_global_config(),
               lp_mint=lp_mint, user_pool_token_account=pf.ata(pa, lp_mint, TOKEN22),
               pool_base_token_account=pf.ata(pool, mint, TOKEN22), pool_quote_token_account=pf.ata(pool, WSOL, TOKEN),
               base_token_program=TOKEN22, quote_token_program=TOKEN, token_2022_program=TOKEN22,
               associated_token_program=ATA_PROG, pump_amm_event_authority=pf.amm_event_authority(), rent=RENT,
               event_authority=pf.event_authority(), program=pf.PUMP)
    idl = json.load(open('/mnt/ssd2/fight_sim/chain/pump_idl.json'))
    spec = next(i for i in idl['instructions'] if i['name'] == 'migrate_v2')
    metas = [AccountMeta(acc['global_' if a['name'] == 'global' else a['name']], bool(a.get('signer')), bool(a.get('writable')))
             for a in spec['accounts']]
    return Instruction(pf.PUMP, bytes(spec['discriminator']), metas + list(remaining))


def simulate_logs(ixs, payer, signers, cu=1_000_000):
    bh = rpc.call('getLatestBlockhash', [{'commitment': 'confirmed'}])['value']['blockhash']
    msg = Message.new_with_blockhash([set_compute_unit_limit(cu)] + list(ixs), payer.pubkey(), Hash.from_string(bh))
    tx = Transaction(list(signers), msg, Hash.from_string(bh))
    sim = rpc.call('simulateTransaction', [base64.b64encode(bytes(tx)).decode(), {'encoding': 'base64', 'commitment': 'confirmed'}])['value']
    return sim.get('err'), sim.get('logs') or []


def boost_vault(mint):
    """PumpSwap's undocumented 'boost' vault: PDA(AMM, ["boost_vault", pool]). migrate_v2's two trailing
    accounts are this vault and its WSOL account; PumpSwap's hidden InitBoost moves part of the curve's
    SOL into it at graduation (17.59 SOL on a real 2026-10-02 migration)."""
    return Pubkey.find_program_address([b'boost_vault', bytes(pf.amm_pool(mint))], pf.AMM)[0]


def graduate_ix(user, mint):
    bv = boost_vault(mint)
    return migrate_v2_ix(user, mint, [AccountMeta(bv, False, True), AccountMeta(pf.ata(bv, WSOL, TOKEN), False, True)])


def amm_buy_exact_ixs(user, mint, sol):
    """PumpSwap buy_exact_quote_in: spend exactly `sol` (wrapped), take whatever base it buys.
    Used by the replay because PumpSwap's fee right after graduation is far above the nominal tier
    (a real first buyer paid ~26% over the pool's constant-product price), so exact-out quoting fails."""
    pool = amm_pool(mint)
    lam = int(sol * LAMPORTS)
    fr, bb = amm_fee_accounts()
    base = pf.amm_buy_ix(user, mint, pool, TOKEN22, fr, bb, 0, 0)
    data = disc('buy_exact_quote_in') + struct.pack('<QQ', lam, 1) + bytes([0])   # min 1 raw token (0 is rejected: ZeroBaseAmount); track_volume = false
    ix = Instruction(pf.AMM, data, list(base.accounts))
    return (pf.wrap_sol_ixs(user, lam)
            + [pf.create_ata_idempotent_ix(user, user, mint, TOKEN22), ix,
               pf.close_ata_ix(user, pf.ata(user, WSOL, TOKEN), TOKEN)])


# ------------------------------------------------------------------ v0 transactions + lookup tables
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
from solders.address_lookup_table_account import AddressLookupTableAccount
ALT_PROG = P('AddressLookupTab1e1111111111111111111111111')


def make_lookup_table(payer, addresses):
    """Create + extend an address lookup table holding `addresses`; returns its AddressLookupTableAccount.
    solders 0.29 has no ALT helper, so CreateLookupTable (0) / ExtendLookupTable (2) are encoded here."""
    slot = rpc.call('getSlot', [{'commitment': 'finalized'}])
    table, bump = Pubkey.find_program_address([bytes(payer.pubkey()), struct.pack('<Q', slot)], ALT_PROG)
    create = Instruction(ALT_PROG, struct.pack('<IQB', 0, slot, bump),
                         [AccountMeta(table, False, True), AccountMeta(payer.pubkey(), True, False),
                          AccountMeta(payer.pubkey(), True, True), AccountMeta(SYSTEM, False, False)])
    send([create], payer, [payer], simulate_first=False)
    uniq = list(dict.fromkeys(addresses))
    for i in range(0, len(uniq), 25):
        chunk = uniq[i:i + 25]
        ext = Instruction(ALT_PROG, struct.pack('<IQ', 2, len(chunk)) + b''.join(bytes(a) for a in chunk),
                          [AccountMeta(table, False, True), AccountMeta(payer.pubkey(), True, False),
                           AccountMeta(payer.pubkey(), True, True), AccountMeta(SYSTEM, False, False)])
        send([ext], payer, [payer], simulate_first=False)
    s0 = rpc.call('getSlot', [{'commitment': 'confirmed'}])
    while rpc.call('getSlot', [{'commitment': 'confirmed'}]) <= s0 + 1:      # entries become usable a slot later
        time.sleep(0.2)
    return AddressLookupTableAccount(table, uniq)


def send_v0(ixs, payer, signers, tables, cu=600_000):
    bh = rpc.call('getLatestBlockhash', [{'commitment': 'confirmed'}])['value']['blockhash']
    budget = [set_compute_unit_limit(cu)] + ([set_compute_unit_price(CU_PRICE)] if CU_PRICE else [])
    msg = MessageV0.try_compile(payer.pubkey(), budget + list(ixs), tables, Hash.from_string(bh))
    tx = VersionedTransaction(msg, list(signers))
    b64 = base64.b64encode(bytes(tx)).decode()
    sim = rpc.call('simulateTransaction', [b64, {'encoding': 'base64', 'commitment': 'confirmed'}])['value']
    if sim.get('err'):
        raise RuntimeError(f'simulation failed: {sim["err"]}\n    ' + '\n    '.join((sim.get('logs') or [])[-25:]))
    sig = rpc.call('sendTransaction', [b64, {'encoding': 'base64', 'skipPreflight': True}])
    t0 = time.time()
    while True:
        try:
            confirm(sig, timeout=3); break
        except TimeoutError:
            if time.time() - t0 > 80: raise
            rpc.call('sendTransaction', [b64, {'encoding': 'base64', 'skipPreflight': True, 'maxRetries': 0}])
    return sig, len(bytes(tx))


def close_lookup_table_ixs(authority, table, step):
    """step 'deactivate' (DeactivateLookupTable = 3), then ~513 slots later 'close' (CloseLookupTable = 4) refunds the rent."""
    if step == 'deactivate':
        return [Instruction(ALT_PROG, struct.pack('<I', 3), [AccountMeta(table, False, True), AccountMeta(authority, True, False)])]
    return [Instruction(ALT_PROG, struct.pack('<I', 4), [AccountMeta(table, False, True), AccountMeta(authority, True, False),
                                                        AccountMeta(authority, False, True)])]
