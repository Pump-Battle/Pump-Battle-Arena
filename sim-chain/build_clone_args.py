"""Rebuild clone.args for the sim validator from TODAY's mainnet: the four programs, their fixed PDAs, pump's and
PumpSwap's current fee recipients (they rotate), and each recipient's WSOL account. Run before every re-clone."""
import json, base64, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'client'))
import pumpfun_frozen as pf
from solders.pubkey import Pubkey
P = Pubkey.from_string
rpc = pf.Rpc('https://api.mainnet-beta.solana.com')
WSOL, TOK, ATA = P('So11111111111111111111111111111111111111112'), P('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'), P('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL')
progs = ['6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P', 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA',
         'pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ', 'MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e']
acct = {k: v[0] for k, v in json.load(open('' + os.path.join(os.path.dirname(os.path.abspath(__file__)), 'const_pdas.json') + '')).items()}
acct.update(pump_fee_config=str(pf.fee_config_pda()), amm_global_config=str(pf.amm_global_config()), amm_event_authority=str(pf.amm_event_authority()),
            amm_fee_config=str(pf.amm_fee_config()), amm_gva=str(pf.amm_global_volume_accumulator()), pump_idl='AYgC53tU5BbP2NAnv5nConJxAdpQZctvmZK88pu69xRs')
raw = lambda a: base64.b64decode(rpc.call('getAccountInfo', [a, {'encoding': 'base64'}])['value']['data'][0])
g = raw(str(pf.global_pda())); c = raw(str(pf.amm_global_config()))
wallets = [pf.authorized_fee_recipient(g), Pubkey(g[162:194])] + pf.buyback_fee_recipients(g)
prot, bb = pf.amm_fee_recipients(c); wallets += prot + bb
for i, w in enumerate(wallets):
    acct[f'w{i}'] = str(w); acct[f'w{i}_wsol'] = str(Pubkey.find_program_address([bytes(w), bytes(TOK), bytes(WSOL)], ATA)[0])
addrs = sorted(set(acct.values()) - set(progs))
exist = []
for i in range(0, len(addrs), 100):
    info = rpc.call('getMultipleAccounts', [addrs[i:i+100], {'encoding': 'base64', 'dataSlice': {'offset': 0, 'length': 0}}])['value']
    exist += [a for a, v in zip(addrs[i:i+100], info) if v is not None]
args = ['--url', 'https://api.mainnet-beta.solana.com'] + sum([['--clone-upgradeable-program', p] for p in progs], []) + sum([['--clone', a] for a in exist], [])
open('' + os.path.join(os.path.dirname(os.path.abspath(__file__)), 'clone.args') + '', 'w').write('\n'.join(args) + '\n')
print(f'{len(exist)} accounts to clone ({len(addrs) - len(exist)} candidates absent on mainnet); fee recipient now {wallets[0]}')
