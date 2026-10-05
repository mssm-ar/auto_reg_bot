#!/usr/bin/env python3
"""
Auto-register the first available hotkey from a list on a subnet.

Behavior:
- Loads env from `.env` (script directory or cwd).
- Checks subnet recycle registration fee each run.
- Registers only if fee <= TARGET_REG_FEE.
- Iterates through `BT_HOTKEY_LIST` (or single `BT_HOTKEY_NAME`) and picks first
  hotkey that exists locally and is not already registered on the subnet.
- Verifies coldkey, hotkey files, unlock status, and coldkey balance before submit.

Intended to run under PM2 every 30 seconds.
"""
import argparse
import asyncio
import io
import os
import sys
from typing import Optional

# Load .env before anything else (no bittensor import yet)
def _early_load_env():
    for base in (os.path.dirname(os.path.abspath(__file__)), os.getcwd()):
        path = os.path.join(base, ".env")
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, _, v = line.partition("=")
                            k, v = k.strip(), v.strip()
                            if k:
                                os.environ.setdefault(k, v)
            except Exception:
                pass
            return

_early_load_env()

# Run in a pty and feed password when we see "Enter your password:" (wallet reads from /dev/tty)
def _run_in_pty_with_password():
    pw = os.environ.get("BT_COLDKEY_PASSWORD") or os.environ.get("COLDKEY_PASSWORD")
    if not pw or os.environ.get("BT_PTY_RUN") or not sys.stdin.isatty():
        return False
    try:
        import pty
        import select
    except ImportError:
        return False
    script = os.path.abspath(__file__)
    argv = [sys.executable, script] + sys.argv[1:]
    env = os.environ.copy()
    env["BT_PTY_RUN"] = "1"
    pid, master_fd = pty.fork()
    if pid == 0:
        try:
            os.close(master_fd)
        except Exception:
            pass
        os.execve(sys.executable, argv, env)
        os._exit(127)
    buf = b""
    prompt = b"Enter your password"
    exit_code = 0
    try:
        while True:
            try:
                ready, _, _ = select.select([master_fd], [], [], 0.05)
                if ready:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    buf += data
                    if prompt in buf:
                        os.write(master_fd, (pw + "\n").encode("utf-8", errors="replace"))
                        buf = buf.replace(prompt, b"", 1)
                    sys.stdout.write(data.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
                try:
                    _pid, status = os.waitpid(pid, os.WNOHANG)
                    if _pid == pid:
                        exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
                        break
                except Exception:
                    pass
            except (OSError, IOError, KeyboardInterrupt):
                break
        try:
            _, status = os.waitpid(pid, 0)
            if exit_code == 0:
                exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
        except Exception:
            pass
    finally:
        try:
            os.close(master_fd)
        except Exception:
            pass
    sys.exit(exit_code)

_run_in_pty_with_password()

def _early_patch_password():
    pw = os.environ.get("BT_COLDKEY_PASSWORD") or os.environ.get("COLDKEY_PASSWORD")
    if not pw:
        return
    import builtins
    _oi = getattr(builtins, "input", None)
    if _oi:
        builtins.input = lambda p="": pw if ("password" in (p or "").lower() or not (p or "").strip()) else _oi(p)
    try:
        import getpass as _gp
        if getattr(_gp, "getpass", None):
            _gp.getpass = lambda prompt="Password: ", stream=None: pw
    except ImportError:
        pass
    sys.stdin = io.StringIO(pw + "\n")

_early_patch_password()

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

import bittensor as bt
from bittensor.utils import UnlockStatus


def _patch_unlock_to_use_env_password():
    """Make bittensor's unlock_key use BT_COLDKEY_PASSWORD from env so no prompt is shown."""
    pw = os.environ.get("BT_COLDKEY_PASSWORD") or os.environ.get("COLDKEY_PASSWORD")
    if not pw:
        return
    _orig = bt.utils.unlock_key

    def _unlock_key(wallet, unlock_type="coldkey", raise_error=False):
        try:
            if unlock_type == "coldkey":
                wallet.get_coldkey(password=pw)
            else:
                wallet.get_hotkey(password=pw)
            return UnlockStatus(True, "")
        except Exception as e:
            if raise_error:
                raise
            return UnlockStatus(False, str(e))

    bt.utils.unlock_key = _unlock_key
    bt.core.types.unlock_key = _unlock_key


def _patch_getpass_and_input():
    """Any getpass or input('password') returns BT_COLDKEY_PASSWORD from env (e.g. wallet native code path)."""
    pw = os.environ.get("BT_COLDKEY_PASSWORD") or os.environ.get("COLDKEY_PASSWORD")
    if not pw:
        return
    import builtins
    _orig_input = getattr(builtins, "input", None)
    if _orig_input:
        def _input(prompt=""):
            s = (prompt or "").lower()
            if "password" in s or not (prompt or "").strip():
                return pw
            return _orig_input(prompt)
        builtins.input = _input
    try:
        import getpass as _gp
        _orig_getpass = getattr(_gp, "getpass", None)
        if _orig_getpass:
            def _getpass(prompt="Password: ", stream=None):
                return pw
            _gp.getpass = _getpass
    except ImportError:
        pass


def _to_tao(value) -> Optional[float]:
    """Best-effort conversion of bittensor values to TAO float."""
    if value is None:
        return None
    tao_attr = getattr(value, "tao", None)
    if tao_attr is not None:
        try:
            return float(tao_attr)
        except Exception:
            pass
    try:
        raw = str(value).replace("τ", "").strip()
        return float(raw)
    except Exception:
        return None


def _parse_hotkey_list() -> list[str]:
    list_raw = (os.environ.get("BT_HOTKEY_LIST") or "").strip()
    if list_raw:
        items = [x.strip() for x in list_raw.split(",")]
        return [x for x in items if x]
    single = (os.environ.get("BT_HOTKEY_NAME") or "").strip()
    if single:
        return [single]
    return []


def _load_env_file(path):
    """Load KEY=VALUE lines from path into os.environ. No quotes or expansion."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    key, value = key.strip(), value.strip()
                    if key:
                        os.environ.setdefault(key, value)
    except Exception:
        pass


def load_env(env_path=None):
    """Load .env from script directory, then cwd, or use given path."""
    def do_load(path):
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            return
        if load_dotenv:
            load_dotenv(path)
        _load_env_file(path)  # fallback: set any key not already in os.environ

    if env_path:
        do_load(env_path)
        return
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for base in (script_dir, os.getcwd()):
        path = os.path.join(base, ".env")
        if os.path.isfile(path):
            do_load(path)
            return


def _wallets_root():
    """Return the wallets directory as an absolute path. Default is Bittensor's ~/.bittensor/wallets.
    Optional BT_WALLETS_PATH in .env: relative paths are resolved from home to form an absolute path."""
    custom = (os.environ.get("BT_WALLETS_PATH") or "").strip()
    if custom:
        custom = os.path.expanduser(custom)
        if not os.path.isabs(custom):
            home = os.path.abspath(os.path.expanduser("~"))
            custom = os.path.join(home, custom)
        return os.path.abspath(os.path.normpath(custom))
    default = os.path.abspath(os.path.expanduser("~/.bittensor/wallets"))
    try:
        w = bt.Wallet(name="default")
        p = getattr(w, "path", None)
        if p:
            p = os.path.expanduser(p)
            if os.path.basename(p) == "default":
                p = os.path.dirname(p)
            return os.path.abspath(p)
    except Exception:
        pass
    return default


def parse_args():
    p = argparse.ArgumentParser(
        description="Register your hotkey (miner) to an existing Bittensor subnet (recycle / registration cost)."
    )
    p.add_argument(
        "--wallet",
        type=str,
        default=None,
        help="Wallet name (optional; overrides BT_WALLET_NAME from .env)",
    )
    p.add_argument(
        "--network",
        type=str,
        default=os.environ.get("BT_NETWORK", "finney"),
        choices=["finney", "test", "local"],
        help="Network to use (default: finney or BT_NETWORK)",
    )
    p.add_argument(
        "--no-mev",
        action="store_true",
        help="Disable MEV Shield (submit transaction without encryption)",
    )
    p.add_argument(
        "--no-wait-finalization",
        action="store_true",
        help="Return after inclusion, do not wait for finalization",
    )
    p.add_argument(
        "--target-reg-fee",
        type=float,
        default=None,
        help="Max recycle fee in TAO allowed for registration (overrides TARGET_REG_FEE)",
    )
    p.add_argument(
        "--interval-secs",
        type=int,
        default=None,
        help="Run loop interval in seconds (overrides CHECK_INTERVAL_SECONDS). 0 = single run.",
    )
    p.add_argument(
        "--env-file",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to .env file (default: .env in current directory)",
    )
    return p.parse_args()


async def run_once(args):
    netuid_str = (os.environ.get("BT_SUBNET_ID") or "").strip()
    if not netuid_str:
        print("Set BT_SUBNET_ID in .env (e.g. 27 for the subnet to register on).")
        return 1
    try:
        netuid = int(netuid_str)
    except ValueError:
        print(f"BT_SUBNET_ID must be a number, got: {netuid_str}")
        return 1

    wallet_name = (args.wallet or os.environ.get("BT_WALLET_NAME", "").strip()) or None
    if not wallet_name:
        print("Set BT_WALLET_NAME in .env or pass --wallet <name>.")
        return 1

    hotkey_names = _parse_hotkey_list()
    if not hotkey_names:
        print("Set BT_HOTKEY_LIST (comma-separated) or BT_HOTKEY_NAME in .env.")
        return 1

    target_fee_raw = (
        args.target_reg_fee
        if args.target_reg_fee is not None
        else os.environ.get("TARGET_REG_FEE", "0.1")
    )
    try:
        target_fee_tao = float(target_fee_raw)
    except Exception:
        print(f"TARGET_REG_FEE must be numeric, got: {target_fee_raw}")
        return 1

    print("--- Setup ---")
    print(f"  Subnet (netuid)   {netuid}")
    print(f"  Wallet            {wallet_name}")
    print(f"  Hotkeys           {', '.join(hotkey_names)}")
    print(f"  Network           {args.network}")
    print(f"  Target reg fee    {target_fee_tao} TAO")

    subtensor = await bt.get_async_subtensor(network=args.network)
    wallet_root = _wallets_root()
    print(f"  Wallet path       {wallet_root}")
    try:
        registration_cost = await subtensor.recycle(netuid=netuid)
        registration_cost_tao = _to_tao(registration_cost)
        if registration_cost is not None:
            print(f"  Recycle cost      {registration_cost}")
        else:
            print("  Recycle cost      (query returned None)")
            return 1
        if registration_cost_tao is None:
            print("Could not convert recycle cost to TAO; skip this cycle.")
            return 1
        if registration_cost_tao > target_fee_tao:
            print(
                f"Recycle cost {registration_cost_tao:.6f} TAO is above target "
                f"{target_fee_tao:.6f} TAO. Skip."
            )
            return 0
    except Exception as e:
        print(f"  Recycle cost      (error: {e})")
        return 1
    finally:
        print()

    selected_wallet = None
    selected_hotkey = None
    selected_ss58 = None
    coldkey_password = os.environ.get("BT_COLDKEY_PASSWORD")
    for hotkey_name in hotkey_names:
        wallet = bt.Wallet(name=wallet_name, hotkey=hotkey_name, path=wallet_root)
        if not wallet.coldkey_file.exists_on_device():
            print(
                f"Coldkey not found for wallet '{wallet_name}'. "
                "Create it first (e.g. btcli wallet new_coldkey)."
            )
            return 1
        if not wallet.hotkey_file.exists_on_device():
            print(f"Hotkey file missing, skip: {hotkey_name}")
            continue

        try:
            if coldkey_password:
                wallet.get_coldkey(password=coldkey_password)
                wallet.get_hotkey(password=coldkey_password)
            else:
                wallet.unlock_coldkey()
                wallet.unlock_hotkey()
        except Exception as e:
            print(f"Unable to unlock hotkey '{hotkey_name}', skip: {e}")
            continue

        try:
            hotkey_ss58 = wallet.hotkey.ss58_address
            uid = await subtensor.get_uid_for_hotkey_on_subnet(hotkey_ss58, netuid)
            if uid is not None and int(uid) >= 0:
                print(f"Hotkey already registered, skip: {hotkey_name} (uid {uid})")
                continue
        except Exception as e:
            print(f"Could not check registration for '{hotkey_name}', skip: {e}")
            continue

        selected_wallet = wallet
        selected_hotkey = hotkey_name
        selected_ss58 = hotkey_ss58
        break

    if not selected_wallet:
        print("No eligible hotkey found (all missing, unlock failed, or already registered).")
        await subtensor.close()
        return 0

    try:
        cold_ss58 = selected_wallet.coldkeypub.ss58_address
    except Exception:
        try:
            selected_wallet.get_coldkeypub()
            cold_ss58 = selected_wallet.coldkeypub.ss58_address
        except Exception as e:
            print(f"Failed to resolve coldkey pubkey: {e}")
            await subtensor.close()
            return 1

    try:
        balance = await subtensor.get_balance(cold_ss58)
        balance_tao = _to_tao(balance)
        fee_tao = _to_tao(registration_cost) or 0.0
        if balance_tao is None:
            print("Could not read balance in TAO; skip this cycle.")
            await subtensor.close()
            return 1
        print(f"  Coldkey balance   {balance}")
        if balance_tao < fee_tao:
            print(
                f"Insufficient balance: need {fee_tao:.6f} TAO, have {balance_tao:.6f} TAO."
            )
            await subtensor.close()
            return 1
    except Exception as e:
        print(f"Failed to check balance: {e}")
        await subtensor.close()
        return 1

    print(f"Attempt registration with hotkey: {selected_hotkey}")
    uid_after = None
    tx_link = None
    result = None
    try:
        result = await subtensor.burned_register(
            selected_wallet,
            netuid,
            mev_protection=not args.no_mev,
            wait_for_inclusion=True,
            wait_for_finalization=not args.no_wait_finalization,
            wait_for_revealed_execution=not args.no_mev,
        )
        if result.success:
            uid_after = await subtensor.get_uid_for_hotkey_on_subnet(selected_ss58, netuid)
            if getattr(result, "extrinsic_receipt", None):
                try:
                    ext_id = await result.extrinsic_receipt.get_extrinsic_identifier()
                    tx_link = f"https://tao.app/extrinsic/{ext_id}"
                except Exception:
                    pass
    finally:
        await subtensor.close()

    if result and result.success:
        print()
        print("=============== REGISTRATION SUCCESSFUL ===============")
        print(f"  Subnet (netuid)   {netuid}")
        print(f"  Hotkey            {selected_hotkey}")
        print(f"  UID               {uid_after}")
        if tx_link:
            print(f"  Transaction       {tx_link}")
        data = getattr(result, "data", None)
        if isinstance(data, dict):
            bal_before = data.get("balance_before")
            bal_after = data.get("balance_after")
            if bal_before is not None and bal_after is not None:
                print(f"  Balance           {bal_before}  →  {bal_after}")
        print("======================================================")
    else:
        print()
        print("--- Registration failed ---")
        msg = getattr(result, "message", None)
        err = getattr(result, "error", None)
        if msg:
            print(f"  {msg}")
        if err is not None and str(err).strip() and str(err) != (msg or "").strip():
            print(f"  {err}")
        if not msg and not (err and str(err).strip()):
            print("  Unknown error (no message returned).")
        return 1
    return 0


async def main():
    load_env()
    _patch_unlock_to_use_env_password()
    _patch_getpass_and_input()
    args = parse_args()
    if args.env_file:
        load_env(args.env_file)

    interval_raw = (
        args.interval_secs
        if args.interval_secs is not None
        else os.environ.get("CHECK_INTERVAL_SECONDS", "0")
    )
    try:
        interval_secs = int(interval_raw)
    except Exception:
        print(f"CHECK_INTERVAL_SECONDS must be an integer, got: {interval_raw}")
        sys.exit(1)

    if interval_secs <= 0:
        code = await run_once(args)
        sys.exit(code)

    print(f"Loop mode enabled. Interval: {interval_secs}s")
    while True:
        code = await run_once(args)
        if code not in (0,):
            print(f"Cycle finished with code={code}; continue loop.")
        await asyncio.sleep(interval_secs)


if __name__ == "__main__":
    asyncio.run(main())
