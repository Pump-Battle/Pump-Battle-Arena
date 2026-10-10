//! Battle Bot Arena — hybrid match contract, Pinocchio build (small enough to deploy for ~0.2-0.3 SOL).
//!
//! Same rules as the Anchor `arena_hybrid`, plus three fixes:
//!   * the sale is KEEPER-ONLY for `keeper_grace` seconds after unlock (it sets a price floor and can send
//!     privately), then anyone may trigger it — so a bot can't trigger the sale with no floor and sandwich it,
//!     and a dead keeper can't strand the bag;
//!   * `close_match` refunds the match account's rent once the prize is settled;
//!   * `sweep_closed` keeps forwarding the coin's creator fees to the creator after the match is closed
//!     (the vault is the coin's pump.fun creator forever), which is why the vault is seeded by (mint, human).
//!
//! Accounts are raw byte layouts; instructions are a 1-byte tag. See `pinolib.py` for the client.

#![no_std]
extern crate alloc;
use alloc::vec::Vec;
use pinocchio::{
    address::Address,
    cpi::{invoke_signed_with_slice, Seed, Signer},
    error::ProgramError,
    instruction::{InstructionAccount, InstructionView},
    sysvars::{clock::Clock, Sysvar},
    AccountView, ProgramResult,
};
use pinocchio_system::instructions::{CreateAccount, Transfer};

pinocchio::program_entrypoint!(process);
pinocchio::default_allocator!();
#[cfg(target_os = "solana")]
#[panic_handler]
fn panic(_: &core::panic::PanicInfo) -> ! {
    // smallest possible handler: no message formatting, the runtime reports the failed instruction
    unsafe { pinocchio::syscalls::abort() }
}

const PUMP: Address = Address::from_str_const("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P");
const PUMP_AMM: Address = Address::from_str_const("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA");
const SPL_TOKEN: Address = Address::from_str_const("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
const SLOT_HASHES: Address = Address::from_str_const("SysvarS1otHashes111111111111111111111111111");

const BUY_V2: [u8; 8] = [184, 23, 238, 97, 103, 197, 211, 61];
const SELL_V2: [u8; 8] = [93, 246, 130, 60, 231, 233, 64, 178];
const AMM_SELL: [u8; 8] = [51, 230, 133, 164, 1, 127, 131, 173];
const COLLECT_CURVE: [u8; 8] = [20, 22, 86, 123, 198, 28, 219, 132];
const COLLECT_AMM: [u8; 8] = [160, 57, 89, 42, 181, 139, 43, 66];

const BPS: u64 = 10_000;
const VAULT_RENT_MIN: u64 = 890_880;
const FLOAT_KEEP: u64 = 10_000_000;

// ---- match account layout (owned by this program, PDA ["match", mint])
const TAG: usize = 0; const VBUMP: usize = 1; const FLAGS: usize = 2; const MBUMP: usize = 3;
const GAIN_BPS: usize = 4; const FEE_BPS: usize = 6;
const HUMAN: usize = 8; const MINT: usize = 40; const SCORER: usize = 72; const KEEPER: usize = 104;
const FALLBACK: usize = 136; const WINNER: usize = 168; const HASH: usize = 200;
const START: usize = 232; const UNLOCK: usize = 240; const LO: usize = 248; const HI: usize = 256;
const CHALLENGE: usize = 264; const TIMEOUT: usize = 272; const GRACE: usize = 280; const CANDLE: usize = 288;
const DEADLINE: usize = 296; const SCORE: usize = 304; const COST: usize = 312; const TOKENS: usize = 320;
const PROCEEDS: usize = 328; const GAIN_PRIZE: usize = 336; const FEE_TOTAL: usize = 344; const FEE_PRIZE: usize = 352;
const POT: usize = 360; const PAID: usize = 368;
const LEN: usize = 376;
const SOLD: u8 = 1; const CLAIMED: u8 = 2; const EXPIRED: u8 = 4;

#[repr(u32)]
enum E { BadParams = 1, BadCoin, NotFresh, BadTrade, NotWholeBag, StillLocked, AlreadySold, AlreadyDrawn,
         NotReady, Closed, WindowOpen, WrongRecipient, NotKeeper, BadAccount, Math }
impl From<E> for ProgramError { fn from(e: E) -> Self { ProgramError::Custom(e as u32) } }

// ---- little helpers
// No `.unwrap()` anywhere: an unwrap keeps the error type's formatting code linked in (~10 KB of core::fmt).
fn arr<const N: usize>(d: &[u8], o: usize) -> [u8; N] {
    match d.get(o..o + N).map(<[u8; N]>::try_from) { Some(Ok(a)) => a, _ => [0u8; N] }
}
fn u64_at(d: &[u8], o: usize) -> u64 { u64::from_le_bytes(arr::<8>(d, o)) }
fn i64_at(d: &[u8], o: usize) -> i64 { i64::from_le_bytes(arr::<8>(d, o)) }
fn u16_at(d: &[u8], o: usize) -> u16 { u16::from_le_bytes(arr::<2>(d, o)) }
fn put(d: &mut [u8], o: usize, v: &[u8]) { d[o..o + v.len()].copy_from_slice(v) }
fn addr(d: &[u8], o: usize) -> &[u8] { &d[o..o + 32] }
fn now() -> Result<i64, ProgramError> { Ok(Clock::get()?.unix_timestamp) }
fn token_amount(a: &AccountView) -> Result<u64, ProgramError> {
    let d = a.try_borrow()?;
    if d.len() < 72 { return Err(E::BadTrade.into()); }
    Ok(u64_at(&d, 64))
}
fn need(c: bool, e: E) -> ProgramResult { if c { Ok(()) } else { Err(e.into()) } }

fn pay(vault: &AccountView, to: &AccountView, amount: u64, signer: &Signer) -> ProgramResult {
    if amount == 0 { return Ok(()); }
    Transfer { from: vault, to, lamports: amount }.invoke_signed(core::slice::from_ref(signer))
}

/// Forward one target instruction (its accounts are the trailing accounts), signed by the vault.
fn forward(program: &Address, prog: &AccountView, data: &[u8], rem: &[AccountView], vault: &Address, signer: &Signer) -> ProgramResult {
    need(prog.address() == program, E::BadTrade)?;
    let metas: Vec<InstructionAccount> = rem.iter()
        .map(|a| InstructionAccount::new(a.address(), a.is_writable(), a.address() == vault)).collect();
    let mut views: Vec<&AccountView> = rem.iter().collect();
    views.push(prog);
    invoke_signed_with_slice(&InstructionView { program_id: program, data, accounts: &metas }, &views, core::slice::from_ref(signer))
}

/// Close the vault's WSOL account into the vault; refund its rent to the caller. Returns the SOL it held.
fn unwrap_wsol(wsol: &AccountView, vault: &AccountView, caller: &AccountView, token: &AccountView, signer: &Signer) -> Result<u64, ProgramError> {
    need(token.address() == &SPL_TOKEN, E::BadTrade)?;
    let amount = token_amount(wsol)?;
    let rent = wsol.lamports().checked_sub(amount).ok_or(E::Math)?;
    let metas = [InstructionAccount::writable(wsol.address()), InstructionAccount::writable(vault.address()),
                 InstructionAccount::readonly_signer(vault.address())];
    invoke_signed_with_slice(&InstructionView { program_id: &SPL_TOKEN, data: &[9], accounts: &metas },
                             &[wsol, vault, vault, token], core::slice::from_ref(signer))?;
    pay(vault, caller, rent, signer)?;
    Ok(amount)
}

/// Check the vault address against its seeds and return the matching signer seeds' bump byte.
fn vault_check(program_id: &Address, vault: &AccountView, mint: &[u8], human: &[u8], bump: u8) -> ProgramResult {
    let a = Address::create_program_address(&[b"vault", mint, human, &[bump]], program_id).map_err(|_| E::BadAccount)?;
    need(vault.address() == &a, E::BadAccount)
}

fn load<'a>(program_id: &Address, m: &'a AccountView) -> Result<pinocchio::account::RefMut<'a, [u8]>, ProgramError> {
    need(m.owned_by(program_id) && m.data_len() == LEN, E::BadAccount)?;
    let mut mm = *m;
    let d = unsafe { core::mem::transmute::<pinocchio::account::RefMut<'_, [u8]>, pinocchio::account::RefMut<'a, [u8]>>(mm.try_borrow_mut()?) };
    need(d[TAG] == 1, E::BadAccount)?;
    Ok(d)
}

fn process(program_id: &Address, accounts: &mut [AccountView], data: &[u8]) -> ProgramResult {
    let (&ix, rest) = data.split_first().ok_or(ProgramError::InvalidInstructionData)?;
    match ix {
        0 => create_match(program_id, accounts, rest),
        1 => sweep(program_id, accounts, rest),
        2 => settle(program_id, accounts, rest),
        3 => draw(program_id, accounts),
        4 => post_result(program_id, accounts, rest),
        5 | 6 => payout(program_id, accounts, ix == 6),
        7 => close_match(program_id, accounts),
        8 => sweep_closed(program_id, accounts, rest),
        _ => Err(ProgramError::InvalidInstructionData),
    }
}

/// accounts: human(s,w) mint curve match(w) vault(w) vault_ata pump system, then buy_v2's accounts
/// data: mbump vbump lock lo hi challenge timeout grace gain_bps fee_bps scorer keeper fallback dev_lamports buy_data..
#[inline(never)]
fn create_match(program_id: &Address, a: &mut [AccountView], d: &[u8]) -> ProgramResult {
    need(a.len() > 8 + 14 && d.len() >= 2 + 48 + 4 + 96 + 8 + 24, E::BadParams)?;
    let (human, mint, curve, m, vault, vault_ata, pump, system) = (a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]);
    let rem = &a[8..];
    need(human.is_signer(), E::BadAccount)?;
    let (mbump, vbump) = (d[0], d[1]);
    let p = &d[2..];
    let (lock, lo, hi) = (i64_at(p, 0), i64_at(p, 8), i64_at(p, 16));
    let (gain_bps, fee_bps) = (u16_at(p, 48), u16_at(p, 50));
    need(lo < hi && hi <= lock && gain_bps as u64 <= BPS && fee_bps as u64 <= BPS, E::BadParams)?;
    let dev = u64_at(p, 148);
    let buy = &p[156..];
    let mint_b: &[u8] = mint.address().as_ref();
    let human_b: &[u8] = human.address().as_ref();
    vault_check(program_id, &vault, mint_b, human_b, vbump)?;
    let ma = Address::create_program_address(&[b"match", mint_b, &[mbump]], program_id).map_err(|_| E::BadAccount)?;
    need(m.address() == &ma && m.is_data_empty(), E::BadAccount)?;
    // the coin must be fresh and its pump.fun creator must be this vault
    {
        need(curve.owned_by(&PUMP), E::BadCoin)?;
        let c = curve.try_borrow()?;
        need(c.len() >= 81, E::BadCoin)?;
        need(u64_at(&c, 32) == 0, E::NotFresh)?;
        need(&c[49..81] == vault.address().as_ref(), E::BadCoin)?;
    }
    // the dev buy, made by the contract
    need(buy.len() >= 24 && buy[..8] == BUY_V2 && u64_at(buy, 16) <= dev, E::BadTrade)?;
    need(rem.len() > 14 && rem[13].address() == vault.address() && rem[14].address() == vault_ata.address()
         && rem[1].address() == mint.address() && rem[10].address() == curve.address(), E::BadTrade)?;
    let mbump_b = [mbump];
    let mseeds = [Seed::from(b"match"), Seed::from(mint_b), Seed::from(&mbump_b)];
    CreateAccount { from: &human, to: &m, lamports: (LEN as u64 + 128) * 6_960, space: LEN as u64, owner: program_id }
        .invoke_signed(&[Signer::from(&mseeds)])?;
    Transfer { from: &human, to: &vault, lamports: dev + FLOAT_KEEP + VAULT_RENT_MIN }.invoke()?;
    let vbump_b = [vbump];
    let vseeds = [Seed::from(b"vault"), Seed::from(mint_b), Seed::from(human_b), Seed::from(&vbump_b)];
    let vs = Signer::from(&vseeds);
    let before = vault.lamports();
    forward(&PUMP, &pump, buy, rem, vault.address(), &vs)?;
    let cost = before.checked_sub(vault.lamports()).ok_or(E::Math)?;
    let tokens = token_amount(&vault_ata)?;
    {   // the tokens really sit in the vault's account for this mint
        let t = vault_ata.try_borrow()?;
        need(&t[0..32] == mint_b && &t[32..64] == vault.address().as_ref(), E::BadAccount)?;
    }
    let excess = vault.lamports().saturating_sub(VAULT_RENT_MIN + FLOAT_KEEP);
    pay(&vault, &human, excess, &vs)?;
    let _ = system;
    let t = now()?;
    let mut md = load(program_id, &m).or_else(|_| -> Result<_, ProgramError> {
        let mut mm = m;
        let r = unsafe { core::mem::transmute::<pinocchio::account::RefMut<'_, [u8]>, pinocchio::account::RefMut<'static, [u8]>>(mm.try_borrow_mut()?) };
        Ok(r)
    })?;
    md[TAG] = 1; md[VBUMP] = vbump; md[MBUMP] = mbump; md[FLAGS] = 0;
    put(&mut md, GAIN_BPS, &gain_bps.to_le_bytes()); put(&mut md, FEE_BPS, &fee_bps.to_le_bytes());
    put(&mut md, HUMAN, human_b); put(&mut md, MINT, mint_b);
    put(&mut md, SCORER, &p[52..84]); put(&mut md, KEEPER, &p[84..116]); put(&mut md, FALLBACK, &p[116..148]);
    put(&mut md, START, &t.to_le_bytes()); put(&mut md, UNLOCK, &(t + lock).to_le_bytes());
    put(&mut md, LO, &lo.to_le_bytes()); put(&mut md, HI, &hi.to_le_bytes());
    put(&mut md, CHALLENGE, &p[24..32]); put(&mut md, TIMEOUT, &p[32..40]); put(&mut md, GRACE, &p[40..48]);
    put(&mut md, COST, &cost.to_le_bytes()); put(&mut md, TOKENS, &tokens.to_le_bytes());
    Ok(())
}

fn split_free(md: &mut [u8], vault: &AccountView, human: &AccountView, vs: &Signer) -> ProgramResult {
    let sold = md[FLAGS] & SOLD != 0;
    let closed = md[FLAGS] & (CLAIMED | EXPIRED) != 0;
    let pot = u64_at(md, POT);
    let reserved = VAULT_RENT_MIN + pot + if sold { 0 } else { FLOAT_KEEP };
    let free = vault.lamports().saturating_sub(reserved);
    if free == 0 { return Ok(()); }
    let to_prize = if closed { 0 } else { free * u16_at(md, FEE_BPS) as u64 / BPS };
    let to_creator = free - to_prize;
    let (ft, fp, pd) = (u64_at(md, FEE_TOTAL) + free, u64_at(md, FEE_PRIZE) + to_prize, u64_at(md, PAID) + to_creator);
    put(md, FEE_TOTAL, &ft.to_le_bytes());
    put(md, FEE_PRIZE, &fp.to_le_bytes());
    put(md, POT, &(pot + to_prize).to_le_bytes());
    put(md, PAID, &pd.to_le_bytes());
    pay(vault, human, to_creator, vs)
}

/// Collect the coin's creator fees (venue 1 curve, 2 PumpSwap) via the trailing accounts.
fn collect(venue: u8, cd: &[u8], target: &AccountView, rem: &[AccountView], vault: &AccountView, wsol: &AccountView,
           caller: &AccountView, token: &AccountView, vs: &Signer) -> ProgramResult {
    if venue == 1 {
        need(cd.len() >= 8 && cd[..8] == COLLECT_CURVE && !rem.is_empty() && rem[0].address() == vault.address(), E::BadTrade)?;
        forward(&PUMP, target, cd, rem, vault.address(), vs)
    } else if venue == 2 {
        need(cd.len() >= 8 && cd[..8] == COLLECT_AMM && rem.len() > 5 && rem[2].address() == vault.address()
             && rem[5].address() == wsol.address(), E::BadTrade)?;
        forward(&PUMP_AMM, target, cd, rem, vault.address(), vs)?;
        unwrap_wsol(wsol, vault, caller, token, vs).map(|_| ())
    } else { Ok(()) }
}

/// accounts: caller(s,w) match(w) human(w) vault(w) wsol(w) target token system, then the collect's accounts
/// data: venue len(u16) collect_data..
#[inline(never)]
fn sweep(program_id: &Address, a: &mut [AccountView], d: &[u8]) -> ProgramResult {
    need(a.len() >= 8 && d.len() >= 3, E::BadParams)?;
    let (caller, m, human, vault, wsol, target, token) = (a[0], a[1], a[2], a[3], a[4], a[5], a[6]);
    let mut md = load(program_id, &m)?;
    need(human.address().as_ref() == addr(&md, HUMAN), E::BadAccount)?;
    vault_check(program_id, &vault, addr(&md, MINT), addr(&md, HUMAN), md[VBUMP])?;
    let (mint_c, human_c, vb) = (arr::<32>(&md, MINT), arr::<32>(&md, HUMAN), [md[VBUMP]]);
    let vseeds = [Seed::from(b"vault"), Seed::from(&mint_c), Seed::from(&human_c), Seed::from(&vb)];
    let vs = Signer::from(&vseeds);
    let n = u16_at(d, 1) as usize;
    need(d.len() >= 3 + n, E::BadParams)?;
    collect(d[0], &d[3..3 + n], &target, &a[8..], &vault, &wsol, &caller, &token, &vs)?;
    split_free(&mut md, &vault, &human, &vs)
}

/// accounts: caller(s,w) match(w) human(w) vault(w) vault_ata(w) wsol(w) target token system, then the sell's accounts
/// data: len(u16) sell_data..
#[inline(never)]
fn settle(program_id: &Address, a: &mut [AccountView], d: &[u8]) -> ProgramResult {
    need(a.len() >= 9 && d.len() >= 2, E::BadParams)?;
    let (caller, m, human, vault, vault_ata, wsol, target, token) = (a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]);
    let rem = &a[9..];
    let mut md = load(program_id, &m)?;
    let t = now()?;
    let unlock = i64_at(&md, UNLOCK);
    need(t >= unlock, E::StillLocked)?;
    need(md[FLAGS] & SOLD == 0, E::AlreadySold)?;
    // anti-sandwich: only the keeper (who sets a price floor and can send privately) during the grace period
    need(t >= unlock + i64_at(&md, GRACE) || (caller.is_signer() && caller.address().as_ref() == addr(&md, KEEPER)), E::NotKeeper)?;
    need(human.address().as_ref() == addr(&md, HUMAN), E::BadAccount)?;
    vault_check(program_id, &vault, addr(&md, MINT), addr(&md, HUMAN), md[VBUMP])?;
    let n = u16_at(d, 0) as usize;
    need(d.len() >= 2 + n && n >= 16, E::BadParams)?;
    let sell = &d[2..2 + n];
    let curve_sale = target.address() == &PUMP && sell[..8] == SELL_V2;
    let amm_sale = target.address() == &PUMP_AMM && sell[..8] == AMM_SELL;
    need(curve_sale || amm_sale, E::BadTrade)?;
    need(u64_at(sell, 8) == u64_at(&md, TOKENS), E::NotWholeBag)?;
    let (u, b) = if curve_sale { (13, 14) } else { (1, 5) };
    need(rem.len() > b.max(6) && rem[u].address() == vault.address() && rem[b].address() == vault_ata.address(), E::BadTrade)?;
    let (mint_c, human_c, vb) = (arr::<32>(&md, MINT), arr::<32>(&md, HUMAN), [md[VBUMP]]);
    let vseeds = [Seed::from(b"vault"), Seed::from(&mint_c), Seed::from(&human_c), Seed::from(&vb)];
    let vs = Signer::from(&vseeds);
    let proceeds = if curve_sale {
        let before = vault.lamports();
        forward(&PUMP, &target, sell, rem, vault.address(), &vs)?;
        vault.lamports().checked_sub(before).ok_or(E::Math)?
    } else {
        need(rem[6].address() == wsol.address(), E::BadTrade)?;
        forward(&PUMP_AMM, &target, sell, rem, vault.address(), &vs)?;
        unwrap_wsol(&wsol, &vault, &caller, &token, &vs)?
    };
    // the creator gets the cost basis back first; gain_bps of the GAIN goes to the prize
    let gain = proceeds.saturating_sub(u64_at(&md, COST));
    let to_prize = gain * u16_at(&md, GAIN_BPS) as u64 / BPS;
    // never pay out more than is free above (rent minimum + the prize owed): the sale itself may have spent
    // part of the float on rent, and anything left over (unswept fees) is split by the next sweep
    let new_pot = u64_at(&md, POT) + to_prize;
    let free = vault.lamports().saturating_sub(VAULT_RENT_MIN + new_pot);
    let to_creator = (proceeds - to_prize + FLOAT_KEEP).min(free);
    pay(&vault, &human, to_creator, &vs)?;
    md[FLAGS] |= SOLD;
    put(&mut md, PROCEEDS, &proceeds.to_le_bytes());
    put(&mut md, GAIN_PRIZE, &to_prize.to_le_bytes());
    let (pot, paid) = (u64_at(&md, POT) + to_prize, u64_at(&md, PAID) + to_creator);
    put(&mut md, POT, &pot.to_le_bytes());
    put(&mut md, PAID, &paid.to_le_bytes());
    Ok(())
}

/// accounts: caller(s) match(w) slot_hashes
fn draw(program_id: &Address, a: &mut [AccountView]) -> ProgramResult {
    need(a.len() >= 3 && a[2].address() == &SLOT_HASHES, E::BadAccount)?;
    let (m, sh) = (a[1], a[2]);
    let mut md = load(program_id, &m)?;
    need(now()? >= i64_at(&md, UNLOCK), E::StillLocked)?;
    need(i64_at(&md, CANDLE) == 0, E::AlreadyDrawn)?;
    let h = sh.try_borrow()?;
    need(h.len() >= 48, E::BadAccount)?;
    let r = u64_at(&h, 16);
    let (lo, hi) = (i64_at(&md, LO), i64_at(&md, HI));
    let end = i64_at(&md, START) + lo + (r % (hi - lo + 1) as u64) as i64;
    put(&mut md, CANDLE, &end.to_le_bytes());
    Ok(())
}

/// accounts: scorer(s) match(w); data: winner(32) score(i64) hash(32)
fn post_result(program_id: &Address, a: &mut [AccountView], d: &[u8]) -> ProgramResult {
    need(a.len() >= 2 && d.len() >= 72, E::BadParams)?;
    let (scorer, m) = (a[0], a[1]);
    let mut md = load(program_id, &m)?;
    need(scorer.is_signer() && scorer.address().as_ref() == addr(&md, SCORER), E::BadAccount)?;
    need(i64_at(&md, CANDLE) != 0 && md[FLAGS] & SOLD != 0, E::NotReady)?;
    need(md[FLAGS] & (CLAIMED | EXPIRED) == 0, E::Closed)?;
    let t = now()?;
    let dl = i64_at(&md, DEADLINE);
    need(dl == 0 || t < dl, E::Closed)?;
    put(&mut md, WINNER, &d[0..32]); put(&mut md, SCORE, &d[32..40]); put(&mut md, HASH, &d[40..72]);
    let ndl = t + i64_at(&md, CHALLENGE);
    put(&mut md, DEADLINE, &ndl.to_le_bytes());
    Ok(())
}

/// accounts: caller(s) match(w) vault(w) recipient(w) system — claim (to the winner) or expire (to the fallback)
fn payout(program_id: &Address, a: &mut [AccountView], expire: bool) -> ProgramResult {
    need(a.len() >= 5, E::BadParams)?;
    let (m, vault, rcpt) = (a[1], a[2], a[3]);
    let mut md = load(program_id, &m)?;
    need(md[FLAGS] & (CLAIMED | EXPIRED) == 0, E::Closed)?;
    let t = now()?;
    let dl = i64_at(&md, DEADLINE);
    if expire {
        need(i64_at(&md, CANDLE) != 0 && dl == 0, E::NotReady)?;
        need(t >= i64_at(&md, UNLOCK) + i64_at(&md, TIMEOUT), E::WindowOpen)?;
        need(rcpt.address().as_ref() == addr(&md, FALLBACK), E::WrongRecipient)?;
    } else {
        need(dl != 0 && t >= dl, E::WindowOpen)?;
        need(rcpt.address().as_ref() == addr(&md, WINNER), E::WrongRecipient)?;
    }
    vault_check(program_id, &vault, addr(&md, MINT), addr(&md, HUMAN), md[VBUMP])?;
    let (mint_c, human_c, vb) = (arr::<32>(&md, MINT), arr::<32>(&md, HUMAN), [md[VBUMP]]);
    let vseeds = [Seed::from(b"vault"), Seed::from(&mint_c), Seed::from(&human_c), Seed::from(&vb)];
    let pot = u64_at(&md, POT);
    // an empty wallet can't be created holding less than the rent minimum: a dust pot to one would fail
    // forever and block close_match, so it stays in the vault as free SOL and goes to the creator's next sweep
    let amount = if rcpt.lamports() == 0 && pot < VAULT_RENT_MIN { 0 } else { pot };
    pay(&vault, &rcpt, amount, &Signer::from(&vseeds))?;
    md[FLAGS] |= if expire { EXPIRED } else { CLAIMED };
    put(&mut md, POT, &0u64.to_le_bytes());
    Ok(())
}

/// accounts: caller(s) human(w) match(w). Anyone may trigger it: the rent can only go back to the stored creator.
fn close_match(program_id: &Address, a: &mut [AccountView]) -> ProgramResult {
    need(a.len() >= 3, E::BadParams)?;
    let (mut human, mut m) = (a[1], a[2]);
    {
        let md = load(program_id, &m)?;
        need(human.address().as_ref() == addr(&md, HUMAN), E::BadAccount)?;
        need(md[FLAGS] & SOLD != 0 && md[FLAGS] & (CLAIMED | EXPIRED) != 0 && u64_at(&md, POT) == 0, E::NotReady)?;
    }
    let l = m.lamports();
    human.set_lamports(human.lamports() + l);
    m.set_lamports(0);
    m.close()
}

/// After close_match: keep forwarding the coin's creator fees to the creator. accounts: caller(s,w) human(w)
/// vault(w) mint match wsol(w) target token system, then the collect's accounts; data: vbump venue len(u16) mbump data..
#[inline(never)]
fn sweep_closed(program_id: &Address, a: &mut [AccountView], d: &[u8]) -> ProgramResult {
    need(a.len() >= 9 && d.len() >= 5, E::BadParams)?;
    let (caller, human, vault, mint, m, wsol, target, token) = (a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]);
    let mint_b: &[u8] = mint.address().as_ref();
    let ma = Address::create_program_address(&[b"match", mint_b, &[d[4]]], program_id).map_err(|_| E::BadAccount)?;
    need(m.address() == &ma && m.lamports() == 0, E::NotReady)?;            // only once the match is closed
    let human_b: &[u8] = human.address().as_ref();
    vault_check(program_id, &vault, mint_b, human_b, d[0])?;
    let vb = [d[0]];
    let vseeds = [Seed::from(b"vault"), Seed::from(mint_b), Seed::from(human_b), Seed::from(&vb)];
    let vs = Signer::from(&vseeds);
    let n = u16_at(d, 2) as usize;
    need(d.len() >= 5 + n, E::BadParams)?;
    collect(d[1], &d[5..5 + n], &target, &a[9..], &vault, &wsol, &caller, &token, &vs)?;
    let free = vault.lamports().saturating_sub(VAULT_RENT_MIN);
    pay(&vault, &human, free, &vs)
}
