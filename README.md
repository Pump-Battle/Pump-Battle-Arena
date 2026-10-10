# Pump Battle Arena

Bots battle over a pump.fun coin for about an hour. The wallet with the best **realized profit** when the bell rings
wins the pot. The creator's dev buy is locked in this program for the whole match and sold by the program when the
match ends. After the creator's single launch transaction, nothing needs a human.

> **Status: live on mainnet at `36ZCF6tSXPc8FpG9wYsVZrWnPNtqUAd7b619MZ8MqKtR`, two test matches paid out, not audited.**
> See [SECURITY.md](SECURITY.md) for what the program trusts and [VERIFY.md](VERIFY.md) to rebuild the exact deployed bytes.

## Matches so far

| # | coin | tier | result |
|---|---|---|---|
| 1 | `oGPKDMCp…EKmrpump` | 15 min | 11 wallets traded; 4 ranked; winner +0.1126 SOL realized; pot 0.0237 SOL paid |
| 2 | `vCj92uzM…BVnpump` | 5 min | 3 ranked; winner +0.0686 SOL realized; pot 0.0014 SOL paid |

Both ran with no human in the loop after the launch transaction: dev buy, lock, fee sweeps, sale, random bell, scoring from the chain, payout, close.

## The rules of a match

1. **Launch.** The creator sends one transaction: it creates the coin on pump.fun with the match **vault** as the coin's
   creator, and the program itself makes the dev buy. The coin must be fresh (nobody bought before the program did).
   The tokens from that buy are locked in the vault, and the cost is recorded on-chain.
2. **The pot grows from what the crowd generates.** The vault is the coin's pump.fun creator, so every creator fee the coin
   earns (on the bonding curve and on PumpSwap, forever) lands in the vault. Anyone can trigger a sweep: **25% of the fees
   go to the pot** until the prize is paid, the rest to the creator.
3. **The lock ends** at a fixed, public time. The program sells the **whole** bag (on the curve, or on PumpSwap if the coin
   graduated). The creator gets the cost of the dev buy back first; **50% of any gain goes to the pot**, the rest to the
   creator. For a short grace period only the keeper may trigger the sale (it sets a price floor so the sale cannot be
   sandwiched); after that anyone may.
4. **The bell.** After the lock ends, anyone can trigger the draw: a random moment between minute 55 and 65 of the match,
   taken from a fresh slot hash. Only trades up to that moment count. Nobody, the creator included, knows it in advance.
5. **Scoring.** Realized profit in SOL: what a wallet received for its sells minus the average cost of the tokens it sold,
   read from the chain's own balance changes (so fees and rent count). Tokens still held count for nothing. Wallets that
   never sold, the vault, and the keeper are excluded. Ties go to the earlier last trade.
6. **Payout.** The scorer posts the winner with a hash of the full leaderboard. After a challenge window (during which
   the scorer may correct it), anyone can trigger the payment of the pot to the winner. If no result is posted within the
   timeout, anyone can send the pot to a fallback address fixed at launch.
7. **Close.** Once the pot is settled, anyone can close the match account; its rent goes back to the creator. Creator fees
   keep flowing to the creator afterwards.

## Instructions

| tag | name | who may call | effect |
|---|---|---|---|
| 0 | `create_match` | the creator | dev buy via CPI into pump.fun `buy_v2`, lock, record cost basis |
| 1 | `sweep` | anyone | collect creator fees (curve or PumpSwap) and split them 25/75 |
| 2 | `settle` | keeper during the grace period, then anyone | sell the whole bag, split the gain 50/50 |
| 3 | `draw` | anyone, after unlock | draw the scoring cut-off |
| 4 | `post_result` | the scorer | winner, score, leaderboard hash |
| 5 | `claim` | anyone, after the challenge window | pay the pot to the winner |
| 6 | `expire` | anyone, after the timeout | pay the pot to the fallback |
| 7 | `close_match` | anyone | refund the match rent to the creator |
| 8 | `sweep_closed` | anyone | forward creator fees to the creator after close |

Accounts: match = PDA `["match", mint]` (376-byte layout in `client/pinolib.py`); vault = PDA `["vault", mint, creator]`,
a system account with no private key that is the coin's pump.fun creator, owns the locked tokens and holds the pot.

## Build

```
cd program
cargo build-sbf --arch v1        # Agave 3.1 rejects sBPF v3
```

The program is written with [Pinocchio](https://github.com/anza-xyz/pinocchio) and is about 43 KB, so its deploy
deposit is about 0.30 SOL. Deploy with `--max-len` set to the exact file size.

## Test

The tests run against a local `solana-test-validator` that clones pump.fun, PumpSwap and their global accounts from
mainnet (`sim-chain/`). Rebuild the clone list first (fee recipients rotate), then:

```
python sim-chain/build_clone_args.py
./sim-chain/fight-validator.sh          # RPC on :18899
solana program deploy --url http://127.0.0.1:18899 program/target/deploy/arena_pino.so --program-id <keypair> --max-len 43048
ARENA_LIB=pinolib python tests/pino_test.py ATTACKS S1 S2 S3
python tests/pino_postclose.py
python tests/pino_fakecreator.py
```

`pino_test.py` runs 16 attack checks (early sale, partial sale, sandwich, double claim, fake scorer, fake creator, ...)
and three full matches driven only by the keeper: a coin that stays on the curve, one that graduates and is sold on
PumpSwap, and one whose scorer never posts.

## Run a keeper

`service/run_keeper.py` drives one or more matches to completion: sweeps, graduates the coin when the curve fills,
sells with a price floor, draws, scores from chain history, posts, claims or expires, closes, and keeps sweeping. The
`service/arena-keeper@.service` unit runs it under systemd; `service/arena-status` prints the state of each match.
Keys are read from `~/.config/arena/keeper.json` and `scorer.json`; the RPC URL from `ARENA_RPC`.

## Layout

```
program/     the on-chain program (Rust, Pinocchio)
site/        the website (a single page that follows the current match)
client/      instruction builders, pump.fun / PumpSwap helpers, the keeper and scorer
service/     keeper service, systemd unit, status command
tests/       attack checks and end-to-end matches on the sim chain
sim-chain/   validator launcher and the mainnet clone list
```

## License

MIT.
