#!/usr/bin/env python3
"""Direct pump.fun chain access: PDA derivation and account decoding.

Why this exists. Everything so far has gone through the ClawPump agent API,
which means a language model reads the account and reports a number in prose.
That is fine for measurement and unacceptable for execution: quote.py already
carries the scar (it parses a DECLARED state line because prose-matching
refused a genuinely clean account). This module talks to the chain.

Nothing here signs or sends. It derives addresses and decodes accounts, so
that every address a later transaction uses has been confirmed to exist
against live chain data BEFORE any funds move.

The derivations are validated, not asserted. birth_events.parquet holds
116,553 CreateEvents in which the chain itself emitted the bonding-curve
address next to the mint; `python pumpfun.py --validate` re-derives every one
of them and requires exact equality. A PDA bug that reaches a buy transaction
sends lamports to an address nobody controls, so the test is total, not a
sample.
"""
from __future__ import annotations

import base64
import struct
import time
from dataclasses import dataclass

import httpx
from solders.pubkey import Pubkey

# ---------------------------------------------------------------- constants

PUMP = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
SYSTEM = Pubkey.from_string("11111111111111111111111111111111")
TOKEN = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
# Current pump.fun mints are Token-2022, NOT legacy SPL Token. Measured
# 2026-09-20: 60 of the 60 most recent births are owned by TokenzQdB. The
# legacy-derived ATA for such a mint does not exist but is a perfectly valid
# address, so getting this wrong fails a buy rather than raising anywhere
# useful. Never assume it -- token_program_for() reads the mint account.
TOKEN_2022 = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
TOKEN_PROGRAMS = (TOKEN, TOKEN_2022)
ATA_PROG = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
RENT = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

# pump.fun launches every token with the same virtual reserves. Decoding the
# global account is only trustworthy if these two land where the layout says
# they do -- see decode_global(), which refuses the account otherwise.
INIT_V_SOL = 30_000_000_000        # 30 SOL
INIT_V_TOK = 1_073_000_000_000_000  # 1.073e9 tokens at 6dp

SOL_DP = 1e9
TOK_DP = 1e6

# ---------------------------------------------------------------------- PDAs
# Seeds are ASCII and version-stable; the derivation is the part that must be
# right, so each one is checked in main() against an account that exists.


def global_pda() -> Pubkey:
    return Pubkey.find_program_address([b"global"], PUMP)[0]


def event_authority() -> Pubkey:
    return Pubkey.find_program_address([b"__event_authority"], PUMP)[0]


def bonding_curve(mint: Pubkey) -> Pubkey:
    """The AMM state account. Exists from the instant the token does."""
    return Pubkey.find_program_address([b"bonding-curve", bytes(mint)], PUMP)[0]


def creator_vault(creator: Pubkey) -> Pubkey:
    """Where the 1% creator fee accrues. Required account on buy and sell."""
    return Pubkey.find_program_address([b"creator-vault", bytes(creator)], PUMP)[0]


def ata(owner: Pubkey, mint: Pubkey, token_program: Pubkey) -> Pubkey:
    """Associated token account. token_program is REQUIRED and has no default
    on purpose: it is part of the seed, and the wrong one silently yields a
    valid address that holds nothing. Get it from token_program_for()."""
    return Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)], ATA_PROG)[0]


def associated_bonding_curve(mint: Pubkey, token_program: Pubkey) -> Pubkey:
    return ata(bonding_curve(mint), mint, token_program)


def token_program_for(rpc: "Rpc", mint: Pubkey) -> Pubkey:
    """Which token program owns this mint. Read, never assumed."""
    acc = rpc.account(mint)
    if acc is None:
        raise ValueError(f"mint {mint} does not exist on chain")
    owner = Pubkey.from_string(acc["owner"])
    if owner not in TOKEN_PROGRAMS:
        raise ValueError(f"mint {mint} owned by {owner}, not a token program")
    return owner


# ------------------------------------------------------------------ decoding


@dataclass
class Global:
    authority: Pubkey
    fee_recipient: Pubkey
    fee_bps: int
    creator_fee_bps: int | None
    raw_len: int


def decode_global(raw: bytes) -> Global:
    """Anchor layout: disc(8) initialized(1) authority(32) fee_recipient(32)
    then five u64 parameters.

    The fee recipient is read, never hardcoded. It has been rotated before,
    and a stale one makes every buy fail on account mismatch -- a failure that
    costs a slot, which is the only thing this strategy has.
    """
    if len(raw) < 113:
        raise ValueError(f"global account too short: {len(raw)}")
    if raw[8] != 1:
        raise ValueError("global account reports initialized=false")
    authority = Pubkey(raw[9:41])
    fee_recipient = Pubkey(raw[41:73])
    v_tok, v_sol, _r_tok, _supply, fee_bps = struct.unpack_from("<QQQQQ", raw, 73)

    # Layout self-check. If these two constants are not where the layout
    # predicts, the struct has moved and fee_recipient above is garbage that
    # would still base58-encode into a plausible-looking address.
    if (v_sol, v_tok) != (INIT_V_SOL, INIT_V_TOK):
        raise ValueError(
            f"global layout check failed: v_sol={v_sol} v_tok={v_tok} "
            f"(expected {INIT_V_SOL}/{INIT_V_TOK}) -- do not trust fee_recipient")

    # Appended by a later program version; absent on older accounts.
    creator_fee_bps = None
    if len(raw) >= 162:
        creator_fee_bps = struct.unpack_from("<Q", raw, 154)[0]
    return Global(authority, fee_recipient, fee_bps, creator_fee_bps, len(raw))


@dataclass
class Curve:
    v_sol: float
    v_tok: float
    r_sol: float
    r_tok: float
    price_sol: float | None
    complete: bool
    creator: Pubkey | None
    cashback: bool = False

    @property
    def price_buy_sol(self) -> float | None:
        """Spot. Not a fill: the constant product means the marginal price
        moves against you inside the same transaction, and the GIJ review's
        BUY_SIDE_PX point is that entry and exit read from opposite sides of
        that. Anything that compares an entry to a live price must say which
        side it is quoting."""
        return self.price_sol


def decode_curve(raw: bytes) -> Curve:
    """disc(8) then u64 vtok, vsol, rtok, rsol, supply, bool complete, and on
    current accounts a creator Pubkey at 49. Same layout memecoin_data/
    track_curves.py has been decoding in production since September."""
    if len(raw) < 49:
        raise ValueError(f"curve account too short: {len(raw)}")
    v_tok, v_sol, r_tok, r_sol, _supply = struct.unpack_from("<QQQQQ", raw, 8)
    complete = bool(raw[48])
    creator = Pubkey(raw[49:81]) if len(raw) >= 81 else None
    # Byte 81 decides WHICH fee recipient the program will accept. Measured
    # 2026-09-20 after a live buy failed: a coin with this set to 1 requires
    # global.reserved_fee_recipient (offset 483) and rejects the regular list
    # at fee_recipient.rs:19; a coin with it 0 requires fee_recipients[] and
    # rejects the reserved one at fee_recipient.rs:35. Reading it costs nothing
    # because we already have the curve account.
    cashback = bool(raw[81]) if len(raw) > 81 else False
    price = (v_sol / SOL_DP) / (v_tok / TOK_DP) if v_tok else None
    return Curve(v_sol / SOL_DP, v_tok / TOK_DP, r_sol / SOL_DP,
                 r_tok / TOK_DP, price, complete, creator, cashback)


# ----------------------------------------------------------------------- rpc

DEFAULT_RPC = "https://api.mainnet-beta.solana.com"


class Rpc:
    """Read-only JSON-RPC. No keypair is ever loaded in this module."""

    def __init__(self, url: str = DEFAULT_RPC, timeout: float = 20.0):
        self.url = url
        self.c = httpx.Client(timeout=timeout,
                              headers={"User-Agent": "pump-battle-arena/0.1"})

    # Transient failures worth another attempt. Everything else is a verdict.
    RETRY_ATTEMPTS = 4
    RETRY_BASE_S = 0.4

    @staticmethod
    def _transient(status: int | None, err: object) -> bool:
        """True only for rate limits and server-side faults.

        A program error -- failed simulation, missing account, NotAuthorized --
        is a real answer and must propagate immediately. Retrying it would turn
        a clean refusal into a delay on a path where delay costs money.
        """
        if status is not None and (status == 429 or 500 <= status < 600):
            return True
        if isinstance(err, dict):
            code = err.get("code")
            if code in (429, -32005):          # -32005: node is behind / limited
                return True
            msg = str(err.get("message", "")).lower()
            return ("rate limit" in msg or "too many requests" in msg
                    or "try again" in msg)
        return False

    def call(self, method: str, params: list):
        last = None
        for attempt in range(self.RETRY_ATTEMPTS):
            status, j = None, None
            try:
                r = self.c.post(self.url,
                                json={"jsonrpc": "2.0", "id": 1,
                                      "method": method, "params": params})
                status = r.status_code
                try:
                    j = r.json()
                except Exception:
                    j = None                   # 429s often return HTML, not JSON
            except Exception as exc:           # connection reset, timeout, DNS
                last = RuntimeError(f"{method}: {type(exc).__name__}: {exc}")
                if attempt == self.RETRY_ATTEMPTS - 1:
                    raise last
                time.sleep(self._backoff(attempt))
                continue

            if j is not None and "error" not in j:
                return j["result"]

            err = (j or {}).get("error")
            if not self._transient(status, err):
                # A real error, or an unparseable response that is not a rate
                # limit. Either way the caller must see it now.
                raise RuntimeError(f"{method}: {err if err else f'HTTP {status}'}")

            last = RuntimeError(f"{method}: {err if err else f'HTTP {status}'}")
            if attempt == self.RETRY_ATTEMPTS - 1:
                break
            time.sleep(self._backoff(attempt))
        raise last

    def _backoff(self, attempt: int) -> float:
        """Exponential, with jitter.

        The jitter matters: the bot and the undertaker poll the SAME public
        endpoint, and synchronised retries are how a rate limit becomes a
        rate-limit storm that blinds the watchdog.
        """
        import random
        return self.RETRY_BASE_S * (2 ** attempt) * (0.5 + random.random())

    def account(self, addr: Pubkey, commitment: str = "confirmed"):
        v = self.call("getAccountInfo",
                      [str(addr), {"encoding": "base64",
                                   "commitment": commitment}])["value"]
        if v is None:
            return None
        return {"owner": v["owner"], "lamports": v["lamports"],
                "data": base64.b64decode(v["data"][0])}

    def accounts(self, addrs: list[Pubkey], commitment: str = "confirmed"):
        out, CH = [], 100
        for i in range(0, len(addrs), CH):
            vs = self.call("getMultipleAccounts",
                           [[str(a) for a in addrs[i:i + CH]],
                            {"encoding": "base64",
                             "commitment": commitment}])["value"]
            for v in vs:
                out.append(None if v is None else
                           {"owner": v["owner"], "lamports": v["lamports"],
                            "data": base64.b64decode(v["data"][0])})
        return out

    def balance_sol(self, addr: Pubkey, commitment: str = "confirmed") -> float:
        return self.call("getBalance", [str(addr), {"commitment": commitment}]
                         )["value"] / SOL_DP

    def token_accounts(self, owner: Pubkey, commitment: str = "confirmed"):
        """Every SPL token the wallet holds. This is the startup-reconciliation
        primitive from the GIJ review item #1: the chain is the source of
        truth about what we are holding, not our own position table."""
        out = []
        # BOTH programs. Querying only legacy SPL Token returns nothing for a
        # current pump.fun position, which would make the startup sweep report
        # a clean book while holding tokens -- the exact silent failure the
        # sweep exists to catch.
        for prog in TOKEN_PROGRAMS:
            res = self.call("getTokenAccountsByOwner",
                            [str(owner), {"programId": str(prog)},
                             {"encoding": "jsonParsed",
                              "commitment": commitment}])
            for it in res["value"]:
                info = it["account"]["data"]["parsed"]["info"]
                amt = info["tokenAmount"]
                out.append({"account": it["pubkey"], "mint": info["mint"],
                            "program": str(prog), "amount": int(amt["amount"]),
                            "ui": float(amt["uiAmountString"])})
        return out


def curve_state(rpc: Rpc, mint: Pubkey):
    acc = rpc.account(bonding_curve(mint))
    if acc is None:
        return None
    if acc["owner"] != str(PUMP):
        raise ValueError(f"curve for {mint} owned by {acc['owner']}, not pump")
    return decode_curve(acc["data"])


# ---------------------------------------------------------------- validation

BIRTHS = "/mnt/ssd2/memecoin_wallets/birth_events.parquet"


def _validate_derivation(limit: int | None = None) -> int:
    """Re-derive every bonding curve the chain has told us about.

    birth_events.parquet is the CreateEvent as emitted by the program: the
    mint and the curve address side by side, decoded by watch_births.py off
    the live websocket. If find_program_address disagrees with the program's
    own output even once, the derivation is wrong and nothing downstream is
    safe. Returns the mismatch count.
    """
    import pyarrow.parquet as pq

    t = pq.read_table(BIRTHS, columns=["mint", "bonding_curve"])
    rows = [(m, c) for m, c in zip(t.column("mint").to_pylist(),
                                   t.column("bonding_curve").to_pylist())
            if m and c]
    if limit:
        rows = rows[:limit]
    bad = 0
    for mint, curve in rows:
        if str(bonding_curve(Pubkey.from_string(mint))) != curve:
            bad += 1
            if bad <= 5:
                print(f"    MISMATCH {mint}: chain={curve} "
                      f"derived={bonding_curve(Pubkey.from_string(mint))}")
    print(f"  bonding-curve PDA: {len(rows) - bad}/{len(rows)} exact match "
          f"against chain-emitted CreateEvents")
    return bad


def _probe(rpc: Rpc, label: str, mints: list[str]) -> None:
    """Confirm the derived accounts actually exist.

    Reads the mint account first, because the token program is a seed of the
    curve ATA and pump.fun uses Token-2022; deriving before reading is how the
    first version of this probe reported every ATA absent.
    """
    if not mints:
        return
    print(f"\n  {label}")
    pks = [Pubkey.from_string(m) for m in mints]
    mint_accs = rpc.accounts(pks)
    curves = [bonding_curve(p) for p in pks]
    curve_accs = rpc.accounts(curves)

    atas, keep = [], []
    for pk, ma in zip(pks, mint_accs):
        if ma is None:
            continue
        prog = Pubkey.from_string(ma["owner"])
        if prog not in TOKEN_PROGRAMS:
            continue
        keep.append(str(pk))
        atas.append(ata(bonding_curve(pk), pk, prog))
    ata_accs = dict(zip(keep, rpc.accounts(atas))) if atas else {}

    for m, pk, ma, cv, ca in zip(mints, pks, mint_accs, curves, curve_accs):
        if ma is None:
            print(f"    {m[:8]}… mint ABSENT")
            continue
        prog = "t22" if ma["owner"] == str(TOKEN_2022) else (
            "spl" if ma["owner"] == str(TOKEN) else ma["owner"][:8])
        if ca is None:
            print(f"    {m[:8]}… [{prog}] curve {str(cv)[:8]}… ABSENT "
                  f"(not a pump.fun token)")
            continue
        st = decode_curve(ca["data"])
        aa = ata_accs.get(m)
        owner_ok = ca["owner"] == str(PUMP)
        print(f"    {m[:8]}… [{prog}] curve {str(cv)[:8]}… "
              f"{'pump' if owner_ok else 'OWNER=' + ca['owner']} len={len(ca['data'])} "
              f"complete={str(st.complete):5} "
              f"px={format(st.price_sol, '.3e') if st.price_sol else '   drained':>9} "
              f"r_sol={st.r_sol:7.3f} curve_ata="
              f"{'ok ' + format(aa['lamports'] / SOL_DP, '.5f') + ' SOL rent' if aa else 'ABSENT'}")


def _check_targets(rpc: Rpc, path: str) -> None:
    """targets.json holds WALLETS to copy, not mints.

    Noted because the first run of this validator derived bonding curves for
    them and reported every one absent, which reads like a broken target list
    and is really a category error. Each entry should be a bare system
    account; anything else means the rebuild wrote the wrong column.
    """
    import json
    try:
        tg = json.load(open(path))
    except FileNotFoundError:
        print(f"  {path} not found -- skipping")
        return
    pks = [Pubkey.from_string(w) for w in tg["targets"]]
    accs = rpc.accounts(pks)
    sysn = sum(1 for a in accs if a and a["owner"] == str(SYSTEM))
    absent = sum(1 for a in accs if a is None)
    other = len(accs) - sysn - absent
    print(f"  {path}: {len(pks)} wallets, rank_hours={tg.get('rank_hours')}, "
          f"generated {tg.get('generated')}")
    print(f"    system-owned wallets: {sysn}   unfunded/absent: {absent}   "
          f"other-program: {other}"
          + ("   <- expected: all system-owned" if other == 0 else
             "   <- UNEXPECTED, check the rebuild"))


def main() -> None:
    import argparse
    import json
    import os

    ap = argparse.ArgumentParser(description="read-only pump.fun validation")
    ap.add_argument("--rpc", default=os.environ.get("SOLANA_HTTP_URL", DEFAULT_RPC))
    ap.add_argument("--limit", type=int, default=None,
                    help="cap the offline derivation test (default: all rows)")
    ap.add_argument("--probe", type=int, default=4,
                    help="live accounts to fetch per group")
    ap.add_argument("--targets", default="pipeline/targets.json")
    a = ap.parse_args()

    rpc = Rpc(a.rpc)
    print(f"rpc: {a.rpc}")

    print("\n[1] program-derived addresses")
    g_pda = global_pda()
    print(f"  global            {g_pda}")
    print(f"  event_authority   {event_authority()}")

    print("\n[2] global account (fee recipient is READ, not assumed)")
    acc = rpc.account(g_pda)
    if acc is None:
        raise SystemExit("  global account does not exist -- wrong program id?")
    print(f"  owner={acc['owner']} {'(pump)' if acc['owner'] == str(PUMP) else 'UNEXPECTED'}"
          f" len={len(acc['data'])}")
    g = decode_global(acc["data"])
    print(f"  layout self-check passed (virtual reserves at expected offsets)")
    print(f"  authority         {g.authority}")
    print(f"  FEE RECIPIENT     {g.fee_recipient}")
    print(f"  fee_bps           {g.fee_bps}"
          f"   creator_fee_bps {g.creator_fee_bps}")
    fr = rpc.account(g.fee_recipient)
    print(f"  fee recipient exists on chain: "
          f"{'yes' if fr else 'NO'} ({(fr['lamports'] / SOL_DP):.4f} SOL)"
          if fr else "  fee recipient: ABSENT")

    print("\n[3] bonding-curve derivation vs chain-emitted ground truth")
    bad = _validate_derivation(a.limit)

    print("\n[4] copy targets are wallets, not mints")
    _check_targets(rpc, a.targets)

    print("\n[5] live account existence")
    import pyarrow.parquet as pq
    t = pq.read_table(BIRTHS, columns=["mint", "creator"])
    born = [(m, c) for m, c in zip(t.column("mint").to_pylist(),
                                   t.column("creator").to_pylist()) if m and c]
    _probe(rpc, "most recent births in the capture (on-curve and dead)",
           [m for m, _ in born[-a.probe:]])
    _probe(rpc, "oldest births in the capture (the graduated/complete state)",
           [m for m, _ in born[:a.probe]])

    # creator_vault is only rent-funded once a fee accrues, so ABSENT here is
    # informative rather than an error: it means that creator has earned
    # nothing yet, and a buy must still pass the derived address.
    cr = Pubkey.from_string(born[-1][1])
    cv = creator_vault(cr)
    got = rpc.account(cv)
    print(f"\n  creator_vault({str(cr)[:8]}…) = {cv}")
    if got:
        print("    exists, {:.6f} SOL".format(got["lamports"] / SOL_DP))
    else:
        print("    absent (no fees accrued yet -- expected, not a fault)")

    print("\n" + ("PASS: derivations match the chain, accounts exist, no funds moved."
                  if bad == 0 else f"FAIL: {bad} PDA mismatches -- do not trade."))


if __name__ == "__main__":
    main()


# ------------------------------------------------------------ instructions
# Built from the program's OWN on-chain Anchor IDL (account
# AYgC53tU5BbP2NAnv5nConJxAdpQZctvmZK88pu69xRs), not from a blog post and not
# from reading one transaction. Two things that reading a transaction alone
# got wrong, and the IDL settled:
#   - a real buy carries 18 accounts but the instruction defines 16; the last
#     two are Anchor remaining_accounts that the client adds. They are not
#     required and they are not derivable, which is why they looked like a
#     failed derivation.
#   - buy and sell SWAP token_program and creator_vault at indices 8 and 9.
# Nothing below signs or sends. These return unsigned instructions.

import hashlib

from solders.instruction import AccountMeta, Instruction

FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")


def anchor_disc(name: str) -> bytes:
    """sha256('global:<name>')[:8], COMPUTED every time.

    This project has been burned once by a pasted discriminator: it looked
    exactly like "no launches" rather than like an error. Never paste one.
    """
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


def fee_config_pda() -> Pubkey:
    """IDL seeds are ['fee_config', <32 const bytes>]; the constant is the
    pump program id, confirmed by deriving it and matching a live buy."""
    return Pubkey.find_program_address([b"fee_config", bytes(PUMP)], FEE_PROGRAM)[0]


def global_volume_accumulator() -> Pubkey:
    return Pubkey.find_program_address([b"global_volume_accumulator"], PUMP)[0]


def user_volume_accumulator(user: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(user)], PUMP)[0]


def create_ata_idempotent_ix(payer: Pubkey, owner: Pubkey, mint: Pubkey,
                             token_program: Pubkey) -> Instruction:
    """Idempotent on purpose: it is a no-op if the account already exists, so
    a retry after an ambiguous send cannot fail on 'account in use'."""
    return Instruction(
        ATA_PROG,
        bytes([1]),
        [AccountMeta(payer, True, True),
         AccountMeta(ata(owner, mint, token_program), False, True),
         AccountMeta(owner, False, False),
         AccountMeta(mint, False, False),
         AccountMeta(SYSTEM, False, False),
         AccountMeta(token_program, False, False)])


def buy_ix(user: Pubkey, mint: Pubkey, creator: Pubkey, token_program: Pubkey,
           fee_recipient: Pubkey, amount: int, max_sol_cost: int,
           track_volume: bool = True) -> Instruction:
    """args: amount u64 (tokens out), max_sol_cost u64 (lamports ceiling),
    track_volume OptionBool (1 byte).

    max_sol_cost is the only thing standing between a thin curve and the whole
    wallet, so it is a required argument with no default."""
    bc = bonding_curve(mint)
    data = (anchor_disc("buy") + amount.to_bytes(8, "little")
            + max_sol_cost.to_bytes(8, "little") + bytes([1 if track_volume else 0]))
    return Instruction(PUMP, data, [
        AccountMeta(global_pda(), False, False),
        AccountMeta(fee_recipient, False, True),
        AccountMeta(mint, False, False),
        AccountMeta(bc, False, True),
        AccountMeta(ata(bc, mint, token_program), False, True),
        AccountMeta(ata(user, mint, token_program), False, True),
        AccountMeta(user, True, True),
        AccountMeta(SYSTEM, False, False),
        AccountMeta(token_program, False, False),      # 8 buy: token_program
        AccountMeta(creator_vault(creator), False, True),
        AccountMeta(event_authority(), False, False),
        AccountMeta(PUMP, False, False),
        AccountMeta(global_volume_accumulator(), False, False),
        AccountMeta(user_volume_accumulator(user), False, True),
        AccountMeta(fee_config_pda(), False, False),
        AccountMeta(FEE_PROGRAM, False, False)])


def sell_ix(user: Pubkey, mint: Pubkey, creator: Pubkey, token_program: Pubkey,
            fee_recipient: Pubkey, amount: int,
            min_sol_output: int = 0) -> Instruction:
    """min_sol_output defaults to 0: when the watchdog is flattening a
    position, getting out matters more than the price, and a slippage floor is
    a way to fail to exit. Pass a floor deliberately if you want one."""
    bc = bonding_curve(mint)
    data = (anchor_disc("sell") + amount.to_bytes(8, "little")
            + min_sol_output.to_bytes(8, "little"))
    return Instruction(PUMP, data, [
        AccountMeta(global_pda(), False, False),
        AccountMeta(fee_recipient, False, True),
        AccountMeta(mint, False, False),
        AccountMeta(bc, False, True),
        AccountMeta(ata(bc, mint, token_program), False, True),
        AccountMeta(ata(user, mint, token_program), False, True),
        AccountMeta(user, True, True),
        AccountMeta(SYSTEM, False, False),
        AccountMeta(creator_vault(creator), False, True),   # 8 sell: vault
        AccountMeta(token_program, False, False),           # 9 sell: token prog
        AccountMeta(event_authority(), False, False),
        AccountMeta(PUMP, False, False),
        AccountMeta(fee_config_pda(), False, False),
        AccountMeta(FEE_PROGRAM, False, False)])


def buy_quote(c: Curve, sol_in_lamports: int, slippage_bps: int = 500,
              fee_bps: int = 100) -> tuple[int, int]:
    """(amount, max_sol_cost) for spending about sol_in_lamports.

    Constant product on the VIRTUAL reserves. The token amount is rounded DOWN
    and the cost ceiling rounded UP, so every rounding error is in the
    wallet's favour rather than the curve's.
    """
    v_sol = int(round(c.v_sol * SOL_DP))
    v_tok = int(round(c.v_tok * TOK_DP))
    if v_sol <= 0 or v_tok <= 0:
        raise ValueError("curve has no virtual reserves (graduated or drained)")
    net = sol_in_lamports * 10_000 // (10_000 + fee_bps)
    amount = (v_tok * net) // (v_sol + net)
    max_sol_cost = (sol_in_lamports * (10_000 + slippage_bps) + 9_999) // 10_000
    return amount, max_sol_cost


# ----------------------------------------------------------- v2 instructions
# Use these, not the legacy buy/sell. With cashback enabled the legacy path
# needs two remaining_accounts the IDL does not document; one of them is
# checked exactly and was not solvable. buy_v2/sell_v2 document every account.
#
# Three things measured by simulation on 2026-09-20, each of which would have
# cost a live failure:
#   - sharing_config derives under the FEE program, NOT under pump. Anchor's
#     ConstraintSeeds error prints the address it expected, which is how this
#     was found; if an account ever fails, read that error before theorising.
#   - the quote is WSOL but NO wrapped-SOL account is needed. Payment comes
#     from native lamports and the WSOL balance is untouched. Passing one
#     wastes ~0.002 SOL of rent and 13k CU.
#   - `amount` is the exact RAW TOKEN amount out, verified 1:1 from 1e6 to
#     8e11. `max_sol_cost` is enforced: an underfunded cap fails the buy.

WSOL = Pubkey.from_string("So11111111111111111111111111111111111111112")


def sharing_config(mint: Pubkey) -> Pubkey:
    """NOTE the program: FEE_PROGRAM, not PUMP."""
    return Pubkey.find_program_address(
        [b"sharing-config", bytes(mint)], FEE_PROGRAM)[0]


def authorized_fee_recipient(global_raw: bytes) -> Pubkey:
    """reserved_fee_recipient at offset 483.

    NOT the `fee_recipient` field at offset 41, and not any of
    fee_recipients[] at 162: all of those are rejected with NotAuthorized by
    the live program. Measured, not assumed. Re-check it -- it rotates.
    """
    return Pubkey(global_raw[483:515])


def fee_recipient_for(curve: "Curve", global_raw: bytes) -> tuple[Pubkey, Pubkey]:
    """(preferred, fallback) fee recipient for THIS coin.

    There is no single right answer: the program accepts a different recipient
    depending on the coin's cashback flag, and picking wrong fails the buy with
    NotAuthorized. Callers should try `preferred` and retry once with
    `fallback` if preflight rejects it -- the rule is right in the cases
    measured, but at least one coin rejected both, so do not trust it blindly.
    """
    reserved = Pubkey(global_raw[483:515])
    regular = Pubkey(global_raw[162:194])
    return (reserved, regular) if curve.cashback else (regular, reserved)


def buyback_fee_recipients(global_raw: bytes) -> list[Pubkey]:
    zero = Pubkey.from_bytes(b"\x00" * 32)
    out = [Pubkey(global_raw[741 + 32 * i:741 + 32 * (i + 1)]) for i in range(8)]
    return [p for p in out if p != zero]


def _v2_metas(user, mint, creator, base_tp, fee_recipient, buyback, include_gva):
    q, bc = TOKEN, bonding_curve(mint)     # quote is WSOL -> legacy SPL Token
    cv, uva = creator_vault(creator), user_volume_accumulator(user)
    m = [AccountMeta(global_pda(), False, False),
         AccountMeta(mint, False, False),
         AccountMeta(WSOL, False, False),
         AccountMeta(base_tp, False, False),
         AccountMeta(q, False, False),
         AccountMeta(ATA_PROG, False, False),
         AccountMeta(fee_recipient, False, True),
         AccountMeta(ata(fee_recipient, WSOL, q), False, True),
         AccountMeta(buyback, False, True),
         AccountMeta(ata(buyback, WSOL, q), False, True),
         AccountMeta(bc, False, True),
         AccountMeta(ata(bc, mint, base_tp), False, True),
         AccountMeta(ata(bc, WSOL, q), False, True),
         AccountMeta(user, True, True),
         AccountMeta(ata(user, mint, base_tp), False, True),
         AccountMeta(ata(user, WSOL, q), False, True),
         AccountMeta(cv, False, True),
         AccountMeta(ata(cv, WSOL, q), False, True),
         AccountMeta(sharing_config(mint), False, False)]
    if include_gva:                        # buy_v2 has it, sell_v2 does not
        m.append(AccountMeta(global_volume_accumulator(), False, False))
    m += [AccountMeta(uva, False, True),
          AccountMeta(ata(uva, WSOL, q), False, True),
          AccountMeta(fee_config_pda(), False, False),
          AccountMeta(FEE_PROGRAM, False, False),
          AccountMeta(SYSTEM, False, False),
          AccountMeta(event_authority(), False, False),
          AccountMeta(PUMP, False, False)]
    return m


def buy_v2_ix(user: Pubkey, mint: Pubkey, creator: Pubkey, base_token_program: Pubkey,
              fee_recipient: Pubkey, buyback: Pubkey, amount: int,
              max_sol_cost: int) -> Instruction:
    return Instruction(
        PUMP,
        anchor_disc("buy_v2") + amount.to_bytes(8, "little")
        + max_sol_cost.to_bytes(8, "little"),
        _v2_metas(user, mint, creator, base_token_program, fee_recipient, buyback, True))


def sell_v2_ix(user: Pubkey, mint: Pubkey, creator: Pubkey, base_token_program: Pubkey,
               fee_recipient: Pubkey, buyback: Pubkey, amount: int,
               min_sol_output: int = 0) -> Instruction:
    """min_sol_output defaults to 0 so a forced exit cannot fail on slippage."""
    return Instruction(
        PUMP,
        anchor_disc("sell_v2") + amount.to_bytes(8, "little")
        + min_sol_output.to_bytes(8, "little"),
        _v2_metas(user, mint, creator, base_token_program, fee_recipient, buyback, False))


# ------------------------------------------------------------- pump AMM
# Once a coin graduates, the bonding curve is complete and sell_v2 cannot exit
# it. The position then lives in a pump-AMM pool and needs this path instead.
# Without it, a token that bonds while we hold it is unsellable by anything in
# this project -- and the measured cost of being stuck is -75% median by 15min.
#
# Built from pump_amm's own on-chain IDL (5fLnXNNoZcZt9Qku6HARM3un3Ttm2cGsR7gN9Zp1R7h3)
# and validated by a simulated buy+sell round trip on a real graduated coin:
# 196k CU, fully sold, -2.47% round trip.

AMM = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
AMM_FEE_CONST = bytes.fromhex(
    "0c14defc825ec67694250818bb654065f4298d3156d571b4d4f8090c18e9a863")


def amm_pool_authority(mint: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([b"pool-authority", bytes(mint)], PUMP)[0]


def amm_pool(mint: Pubkey, quote: Pubkey = WSOL, index: int = 0) -> Pubkey:
    """Seeds are [b"pool", index u16 LE, pool_authority, base_mint, quote_mint].
    Verified against two graduated coins: the decoded pool's base_mint matches."""
    return Pubkey.find_program_address(
        [b"pool", index.to_bytes(2, "little"), bytes(amm_pool_authority(mint)),
         bytes(mint), bytes(quote)], AMM)[0]


def amm_pool_v2(mint: Pubkey) -> Pubkey:
    """A placeholder that does NOT exist on chain for normal pools, but the
    program checks its address exactly and rejects the trade without it
    (InvalidPoolV2, 6062). Solved by brute force against a real on-chain sell;
    note the seed is "pool-v2" with a HYPHEN, under the AMM program."""
    return Pubkey.find_program_address([b"pool-v2", bytes(mint)], AMM)[0]


def amm_global_config() -> Pubkey:
    return Pubkey.find_program_address([b"global_config"], AMM)[0]


def amm_event_authority() -> Pubkey:
    return Pubkey.find_program_address([b"__event_authority"], AMM)[0]


def amm_creator_vault_authority(coin_creator: Pubkey) -> Pubkey:
    """Note the seed is "creator_vault" with an UNDERSCORE here, while the
    bonding curve uses "creator-vault" with a hyphen. They are different
    programs and the inconsistency is theirs, not ours."""
    return Pubkey.find_program_address(
        [b"creator_vault", bytes(coin_creator)], AMM)[0]


def amm_fee_config() -> Pubkey:
    return Pubkey.find_program_address([b"fee_config", AMM_FEE_CONST], FEE_PROGRAM)[0]


@dataclass
class AmmPool:
    pool: Pubkey
    base_mint: Pubkey
    quote_mint: Pubkey
    base_ta: Pubkey
    quote_ta: Pubkey
    coin_creator: Pubkey


def amm_pool_state(rpc: Rpc, mint: Pubkey, quote: Pubkey = WSOL) -> AmmPool | None:
    p = amm_pool(mint, quote)
    acc = rpc.account(p)
    if acc is None:
        return None
    if acc["owner"] != str(AMM):
        raise ValueError(f"pool {p} owned by {acc['owner']}, not pump_amm")
    d = acc["data"]
    return AmmPool(p, Pubkey(d[43:75]), Pubkey(d[75:107]), Pubkey(d[139:171]),
                   Pubkey(d[171:203]), Pubkey(d[211:243]))


def amm_fee_recipients(gc_raw: bytes) -> tuple[list[Pubkey], list[Pubkey]]:
    """(protocol, buyback). NOTE the AMM accepts protocol_fee_recipients[8] at
    offset 57 and REJECTS reserved_fee_recipient -- the exact opposite of the
    bonding-curve program, which only accepts its reserved one. Measured."""
    zero = Pubkey.from_bytes(b"\x00" * 32)
    prot = [Pubkey(gc_raw[57 + 32 * i:57 + 32 * (i + 1)]) for i in range(8)]
    buyb = [Pubkey(gc_raw[643 + 32 * i:643 + 32 * (i + 1)]) for i in range(8)]
    return [p for p in prot if p != zero], [p for p in buyb if p != zero]


def amm_sell_ix(user: Pubkey, mint: Pubkey, pool: AmmPool, base_token_program: Pubkey,
                protocol_fee_recipient: Pubkey, buyback: Pubkey,
                base_amount_in: int, min_quote_out: int = 0) -> Instruction:
    """Exit a graduated position. Proceeds arrive as WRAPPED SOL in the user's
    quote ATA, so the caller must create that ATA and should close it
    afterwards to recover native SOL -- unlike the bonding curve, which pays
    native lamports directly."""
    q = TOKEN                                   # WSOL is legacy SPL Token
    ca = amm_creator_vault_authority(pool.coin_creator)
    metas = [AccountMeta(pool.pool, False, True),
             AccountMeta(user, True, True),
             AccountMeta(amm_global_config(), False, False),
             AccountMeta(mint, False, False),
             AccountMeta(pool.quote_mint, False, False),
             AccountMeta(ata(user, mint, base_token_program), False, True),
             AccountMeta(ata(user, pool.quote_mint, q), False, True),
             AccountMeta(pool.base_ta, False, True),
             AccountMeta(pool.quote_ta, False, True),
             AccountMeta(protocol_fee_recipient, False, False),
             AccountMeta(ata(protocol_fee_recipient, pool.quote_mint, q), False, True),
             AccountMeta(base_token_program, False, False),
             AccountMeta(q, False, False),
             AccountMeta(SYSTEM, False, False),
             AccountMeta(ATA_PROG, False, False),
             AccountMeta(amm_event_authority(), False, False),
             AccountMeta(AMM, False, False),
             AccountMeta(ata(ca, pool.quote_mint, q), False, True),
             AccountMeta(ca, False, False),
             AccountMeta(amm_fee_config(), False, False),
             AccountMeta(FEE_PROGRAM, False, False),
             # remaining_accounts, in this order. Undocumented in the IDL and
             # read off a real on-chain sell.
             AccountMeta(amm_pool_v2(mint), False, False),
             AccountMeta(buyback, False, True),
             AccountMeta(ata(buyback, pool.quote_mint, q), False, True)]
    return Instruction(
        AMM,
        anchor_disc("sell") + base_amount_in.to_bytes(8, "little")
        + min_quote_out.to_bytes(8, "little"),
        metas)


def close_ata_ix(owner: Pubkey, account: Pubkey, token_program: Pubkey) -> Instruction:
    """SPL Token CloseAccount (9). Unwraps WSOL back to native SOL and reclaims
    the rent; without it AMM proceeds sit in a token account."""
    return Instruction(token_program, bytes([9]),
                       [AccountMeta(account, False, True),
                        AccountMeta(owner, False, True),
                        AccountMeta(owner, True, False)])
# ─────────────────────────────────────────────────────────────────────────────
# p31: AMM BUY. Append to pumpfun.py, after amm_sell_ix.
#
# WHY THIS DID NOT EXIST. We built amm_sell_ix so a coin that GRADUATES WHILE
# HELD is still exitable -- being stuck costs -75% median by 15 minutes. We
# never built the entry, so the bot can only buy on the bonding curve. That
# makes the whole system a pre-graduation trader: it buys births and treats
# graduation as an exit. The liquid market -- where a copied wallet buys coins
# 14-20 HOURS old through an AMM -- was unreachable, not by decision but by
# omission.
#
# EXACT-OUT, WHICH IS THE SAFETY PROPERTY. pump_amm's buy takes
# (base_amount_out, max_quote_amount_in). We ask for a conservative base amount
# and cap the spend at exactly the lamports we wrapped. If the price moves
# against us the program needs more than the cap and the tx FAILS -- it cannot
# silently overspend. If it moves for us we simply pay less.
#
# WSOL, unlike the curve. The bonding curve takes native lamports; the AMM
# takes WRAPPED SOL from the user's quote ATA. So a buy must create that ATA,
# transfer lamports into it, sync_native to register them, and close it
# afterwards to recover both the change and the rent.
# ─────────────────────────────────────────────────────────────────────────────


def sync_native_ix(account: Pubkey) -> Instruction:
    """SPL Token SyncNative (17): make the token account's `amount` reflect
    lamports transferred in. Without it the wrapped SOL is invisible to the AMM."""
    return Instruction(TOKEN, bytes([17]), [AccountMeta(account, False, True)])


def wrap_sol_ixs(owner: Pubkey, lamports: int) -> list[Instruction]:
    """create (idempotent) -> fund -> sync. Returns the ixs; the caller closes."""
    wa = ata(owner, WSOL, TOKEN)
    return [create_ata_idempotent_ix(owner, owner, WSOL, TOKEN),
            Instruction(SYSTEM,
                        bytes([2, 0, 0, 0]) + lamports.to_bytes(8, "little"),
                        [AccountMeta(owner, True, True),
                         AccountMeta(wa, False, True)]),
            sync_native_ix(wa)]


def _ta_amount(rpc: "Rpc", account: Pubkey) -> int | None:
    """SPL token account `amount` is a u64 LE at offset 64."""
    acc = rpc.account(account)
    if acc is None:
        return None
    d = acc["data"]
    return int.from_bytes(d[64:72], "little") if len(d) >= 72 else None


def amm_buy_quote(rpc: "Rpc", pool: AmmPool, sol_lamports: int,
                  slippage_bps: int = 500,
                  fee_bps: int = 30) -> tuple[int, int] | None:
    """(base_amount_out, max_quote_amount_in) from live pool reserves.

    Constant product on the pool's own token accounts. fee_bps is the total
    taken by the AMM (lp + protocol + creator); 30bps is deliberately
    GENEROUS -- overstating the fee understates base_out, which can only make
    the trade fail rather than overspend. max_quote_in is pinned to exactly
    what we wrap, so the cap is real regardless of this estimate.
    """
    base_r = _ta_amount(rpc, pool.base_ta)
    quote_r = _ta_amount(rpc, pool.quote_ta)
    if not base_r or not quote_r:
        return None
    qin = sol_lamports * (10_000 - fee_bps) // 10_000
    base_out = base_r * qin // (quote_r + qin)
    if base_out <= 0:
        return None
    base_out = base_out * (10_000 - slippage_bps) // 10_000
    return (base_out, sol_lamports) if base_out > 0 else None


def amm_global_volume_accumulator() -> Pubkey:
    """Same seed as the bonding curve's, but under the AMM program.

    Found by simulation: pump_amm's Buy checks this slot and reported
    AccountOwnedByWrongProgram (3007) "Left: pfeeUxB6... Right: pAMMBay6..."
    when fee_config sat here instead -- i.e. it wants an AMM-owned account.
    """
    return Pubkey.find_program_address([b"global_volume_accumulator"], AMM)[0]


def amm_user_volume_accumulator(user: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(user)], AMM)[0]


def amm_buy_ix(user: Pubkey, mint: Pubkey, pool: AmmPool,
               base_token_program: Pubkey, protocol_fee_recipient: Pubkey,
               buyback: Pubkey, base_amount_out: int,
               max_quote_in: int) -> Instruction:
    """Enter a GRADUATED coin through the pump AMM.

    THE ACCOUNT LIST IS *NOT* THE SAME AS amm_sell_ix. Buy inserts TWO extra
    accounts -- global_volume_accumulator and user_volume_accumulator -- ahead
    of fee_config. Assuming buy and sell shared a list produced
    AccountOwnedByWrongProgram (3007) naming global_volume_accumulator, because
    fee_config (owned by the fee program) was sitting in that slot.

    Still required, as on the sell side: the three undocumented
    remaining_accounts (pool_v2, buyback, buyback's quote ATA), which are
    rejected as InvalidPoolV2 (6062) if omitted. Do not "tidy" them away.

    Args are (base_amount_out, max_quote_amount_in) -- the OPPOSITE order of
    sell's (base_amount_in, min_quote_out).
    """
    q = TOKEN                                   # WSOL is legacy SPL Token
    ca = amm_creator_vault_authority(pool.coin_creator)
    metas = [AccountMeta(pool.pool, False, True),
             AccountMeta(user, True, True),
             AccountMeta(amm_global_config(), False, False),
             AccountMeta(mint, False, False),
             AccountMeta(pool.quote_mint, False, False),
             AccountMeta(ata(user, mint, base_token_program), False, True),
             AccountMeta(ata(user, pool.quote_mint, q), False, True),
             AccountMeta(pool.base_ta, False, True),
             AccountMeta(pool.quote_ta, False, True),
             AccountMeta(protocol_fee_recipient, False, False),
             AccountMeta(ata(protocol_fee_recipient, pool.quote_mint, q), False, True),
             AccountMeta(base_token_program, False, False),
             AccountMeta(q, False, False),
             AccountMeta(SYSTEM, False, False),
             AccountMeta(ATA_PROG, False, False),
             AccountMeta(amm_event_authority(), False, False),
             AccountMeta(AMM, False, False),
             AccountMeta(ata(ca, pool.quote_mint, q), False, True),
             AccountMeta(ca, False, False),
             AccountMeta(amm_global_volume_accumulator(), False, True),
             AccountMeta(amm_user_volume_accumulator(user), False, True),
             AccountMeta(amm_fee_config(), False, False),
             AccountMeta(FEE_PROGRAM, False, False),
             AccountMeta(amm_pool_v2(mint), False, False),
             AccountMeta(buyback, False, True),
             AccountMeta(ata(buyback, pool.quote_mint, q), False, True)]
    return Instruction(
        AMM,
        anchor_disc("buy") + base_amount_out.to_bytes(8, "little")
        + max_quote_in.to_bytes(8, "little"),
        metas)
