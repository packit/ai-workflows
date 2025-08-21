import os
import re
import subprocess


def extract_principal(keytab_file: str) -> str | None:
    """
    Extracts principal from the specified keytab file. Assumes that there is
    a single principal in the keytab.

    Args:
        keytab_file: Path to a keytab file.

    Returns:
        Extracted principal.
    """
    proc = subprocess.run(["klist", "-k", "-K", "-e", keytab_file], capture_output=True, text=True)
    print(proc.stdout)
    if proc.returncode:
        print(proc.stderr)
        return None
    key_pattern = re.compile(r"^\s*(\d+)\s+(\S+)\s+\((\S+)\)\s+\((\S+)\)$")
    for line in proc.stdout.splitlines():
        if not (match := key_pattern.match(line)):
            continue
        # just return the principal associated with the first key
        return match.group(2)
    return None


def init_kerberos_ticket() -> bool:
    """
    Initializes Kerberos ticket unless it's already present in a credentials cache.
    """
    keytab_file = os.getenv("KEYTAB_FILE")
    principal = extract_principal(keytab_file)
    if not principal:
        print("Failed to extract principal")
        return False
    proc = subprocess.run(["klist", "-l"], capture_output=True, text=True)
    print(proc.stdout)
    if proc.returncode:
        print(proc.stderr)
    elif principal in proc.stdout:
        return True
    env = os.environ.copy()
    env.update({"KRB5_TRACE": "/dev/stdout"})
    return not subprocess.run(["kinit", "-k", "-t", keytab_file, principal], env=env).returncode
