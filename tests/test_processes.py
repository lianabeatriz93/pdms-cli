"""Process handling must behave the same on Windows, macOS and Linux."""

from __future__ import annotations

import subprocess
import sys
import time

import psutil

from pdms_cli import instances

# A parent that starts a child and both sleep, like the uvicorn reloader and its worker.
TREE = (
    "import subprocess, sys, time;"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
    "time.sleep(60)"
)


def wait_for_children(pid: int, count: int = 1, timeout: float = 10) -> list[psutil.Process]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        children = psutil.Process(pid).children(recursive=True)
        if len(children) >= count:
            return children
        time.sleep(0.1)
    raise AssertionError("the child process did not start")


def test_kill_tree_stops_the_process_and_its_children():
    proc = subprocess.Popen([sys.executable, "-c", TREE], **instances.detach_options())
    children = wait_for_children(proc.pid)
    created = instances.creation_time(proc.pid)
    assert instances.process_alive(proc.pid, created)

    instances.kill_tree(proc.pid, created, timeout=5)
    proc.wait(timeout=10)
    assert not instances.process_alive(proc.pid, created)
    assert not any(child.is_running() and child.status() != psutil.STATUS_ZOMBIE for child in children)


def test_a_reused_pid_is_not_mistaken_for_our_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        created = instances.creation_time(proc.pid)
        assert instances.process_alive(proc.pid, created)
        assert not instances.process_alive(proc.pid, created - 3600)  # same PID, different process
        instances.kill_tree(proc.pid, created - 3600)  # must not touch it
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


def test_finished_processes_are_not_alive():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not instances.process_alive(proc.pid)
    assert not instances.process_alive(2**22 + 12345)  # a PID that does not exist


def test_detach_options_match_the_platform():
    options = instances.detach_options()
    if sys.platform == "win32":
        assert options["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert options == {"start_new_session": True}
