#!/usr/bin/env python3
"""
Helper for SSH_ASKPASS: prints SSH_KEY_PASSPHRASE from environment.
Used by new_branch.py and commit_push.py when SSH key has a passphrase.
"""
import os
import sys

if __name__ == "__main__":
    passphrase = os.environ.get("SSH_KEY_PASSPHRASE", "")
    sys.stdout.write(passphrase)
    sys.stdout.flush()
