import asyncio
import os
import re
import subprocess


async def extract_principal(keytab_file: str) -> str | None:
    """
    Extracts principal from the specified keytab file. Assumes that there is
    a single principal in the keytab.

    Args:
        keytab_file: Path to a keytab file.

    Returns:
        Extracted principal.
    """
    proc = await asyncio.create_subprocess_exec(
        "klist", "-k", "-K", "-e", keytab_file,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    print(stdout.decode(), flush=True)
    if proc.returncode:
        print(stderr.decode(), flush=True)
        return None
    key_pattern = re.compile(r"^\s*(\d+)\s+(\S+)\s+\((\S+)\)\s+\((\S+)\)$")
    for line in stdout.decode().splitlines():
        if not (match := key_pattern.match(line)):
            continue
        # just return the principal associated with the first key
        return match.group(2)
    return None


async def init_kerberos_ticket() -> bool:
    """
    Initializes Kerberos ticket unless it's already present in a credentials cache.
    """
    keytab_file = os.getenv("KEYTAB_FILE")
    principal = await extract_principal(keytab_file)
    if not principal:
        print("Failed to extract principal", flush=True)
        return False
    proc = await asyncio.create_subprocess_exec(
        "klist", "-l",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    print(stdout.decode(), flush=True)
    if proc.returncode:
        print(stderr.decode(), flush=True)
    elif any(l for l in stdout.decode().splitlines() if principal in l and "Expired" not in l):
        return True
    env = os.environ.copy()
    env.update({"KRB5_TRACE": "/dev/stdout"})
    proc = await asyncio.create_subprocess_exec("kinit", "-k", "-t", keytab_file, principal, env=env)
    return not await proc.wait()
