"""The ``credential_process`` of the AWS config pdms gives its services (see :func:`awsenv.credentials_env`).

Services pin an old botocore that cannot refresh the AWS CLI's SSO session: an hour after ``aws sso login`` it says
"Token has expired and refresh failed" while the CLI still works. So the services ask the CLI for credentials
(``aws configure export-credentials``), with the user's own AWS config instead of pdms's, and botocore asks again
before they expire.

Usage: ``python -E -m pdms_cli.awscreds <profile> <the user's AWS config>``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def main(argv: list[str] | None = None) -> int:
    profile, config = argv if argv is not None else sys.argv[1:3]
    tool = shutil.which("aws")
    if not tool:
        print("pdms: the AWS CLI (aws) is not installed", file=sys.stderr)
        return 1
    env = {**os.environ, "AWS_CONFIG_FILE": config}
    env.pop("AWS_PROFILE", None)
    return subprocess.run([tool, "configure", "export-credentials", "--profile", profile, "--format", "process"],
                          env=env, stdin=subprocess.DEVNULL).returncode


if __name__ == "__main__":
    sys.exit(main())
