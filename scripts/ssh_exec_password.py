from __future__ import annotations

import argparse
import getpass
import shlex
import sys
import time
from pathlib import Path

import paramiko


def resolve_ssh_config(host: str, user: str | None, port: int | None) -> tuple[str, str, int]:
    hostname = host
    resolved_user = user
    resolved_port = port

    config_path = Path.home() / ".ssh" / "config"
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as handle:
            config = paramiko.SSHConfig()
            config.parse(handle)
        entry = config.lookup(host)
        hostname = entry.get("hostname", hostname)
        resolved_user = resolved_user or entry.get("user")
        if resolved_port is None and entry.get("port"):
            resolved_port = int(entry["port"])

    return hostname, resolved_user or getpass.getuser(), resolved_port or 22


def read_password(path: str) -> str:
    password = Path(path).expanduser().read_text(encoding="utf-8")
    return password.rstrip("\r\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a remote shell script over SSH password auth.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="")
    parser.add_argument("--port", type=int)
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--command", default="sh -s")
    parser.add_argument("--provide-sudo-password", action="store_true")
    args = parser.parse_args()

    password = read_password(args.password_file)
    remote_script = sys.stdin.read()
    if args.provide_sudo_password:
        remote_script = f"SUDO_PASSWORD={shlex.quote(password)}\n" + remote_script

    hostname, username, port = resolve_ssh_config(args.host, args.user or None, args.port)

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=hostname,
            port=port,
            username=username,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=15,
            auth_timeout=15,
            banner_timeout=15,
        )

        channel = client.get_transport().open_session()
        channel.exec_command(args.command)
        channel.sendall(remote_script.encode("utf-8"))
        channel.shutdown_write()

        while True:
            if channel.recv_ready():
                sys.stdout.buffer.write(channel.recv(65536))
                sys.stdout.buffer.flush()
            if channel.recv_stderr_ready():
                sys.stderr.buffer.write(channel.recv_stderr(65536))
                sys.stderr.buffer.flush()
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            time.sleep(0.05)

        return channel.recv_exit_status()
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
