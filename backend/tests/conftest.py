"""Shared test-suite environment.

Hermeticity note: the provider tests talk to in-process stub servers on
``127.0.0.1``. On hosts with a system HTTP proxy configured (e.g. macOS
network settings), :mod:`urllib` honours it — and a proxy that does not
bypass the *literal* ``127.0.0.1`` closes the loopback connection
("Remote end closed connection without response"). Python's urllib prefers
``*_proxy`` environment variables over the OS config when any is set, so
pinning ``no_proxy`` here makes the suite immune to whether the developer
machine's proxy app happens to be running. Tests never leave the machine;
this does not weaken hermeticity.
"""
import os

os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
