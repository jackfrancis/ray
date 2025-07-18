import json
import os
import subprocess
import sys
import unittest
import pexpect
from pexpect.popen_spawn import PopenSpawn
from telnetlib import Telnet
from typing import Union

import pytest

import ray
from ray._common.test_utils import wait_for_condition
from ray._private import ray_constants, services
from ray._private.test_utils import run_string_as_driver
from ray.cluster_utils import Cluster, cluster_not_supported


def _pexpect_spawn(cmd: str) -> Union["pexpect.spawn", PopenSpawn]:
    """Handles compatibility for pexpect on Windows."""
    if sys.platform == "win32":
        p = PopenSpawn(cmd, encoding="utf-8")
    else:
        p = pexpect.spawn(cmd)

    return p


def test_ray_debugger_breakpoint(shutdown_only):
    ray.init(num_cpus=1, runtime_env={"env_vars": {"RAY_DEBUG": "legacy"}})

    @ray.remote
    def f():
        ray.util.pdb.set_trace()
        return 1

    result = f.remote()

    wait_for_condition(
        lambda: len(
            ray.experimental.internal_kv._internal_kv_list(
                "RAY_PDB_", namespace=ray_constants.KV_NAMESPACE_PDB
            )
        )
        > 0
    )
    active_sessions = ray.experimental.internal_kv._internal_kv_list(
        "RAY_PDB_", namespace=ray_constants.KV_NAMESPACE_PDB
    )
    assert len(active_sessions) == 1

    # Now continue execution:
    session = json.loads(
        ray.experimental.internal_kv._internal_kv_get(
            active_sessions[0], namespace=ray_constants.KV_NAMESPACE_PDB
        )
    )
    host, port = session["pdb_address"].split(":")
    assert host == "localhost"  # Should be private by default.

    tn = Telnet(host, int(port))
    tn.write(b"c\n")

    # The message above should cause this to return now.
    ray.get(result)


def test_ray_debugger_commands(shutdown_only):
    ray.init(num_cpus=2, runtime_env={"env_vars": {"RAY_DEBUG": "legacy"}})

    @ray.remote
    def f():
        """We support unicode too: 🐛"""
        ray.util.pdb.set_trace()

    result1 = f.remote()
    result2 = f.remote()

    wait_for_condition(
        lambda: len(
            ray.experimental.internal_kv._internal_kv_list(
                "RAY_PDB_", namespace=ray_constants.KV_NAMESPACE_PDB
            )
        )
        > 0
    )

    # Make sure that calling "continue" in the debugger
    # gives back control to the debugger loop:
    p = _pexpect_spawn("ray debug")
    p.expect("Enter breakpoint index or press enter to refresh: ")
    p.sendline("0")
    p.expect("-> ray.util.pdb.set_trace()")
    p.sendline("ll")
    # Cannot use the 🐛 symbol here because pexpect doesn't support
    # unicode, but this test also does nicely:
    p.expect("unicode")
    p.sendline("c")
    p.expect("Enter breakpoint index or press enter to refresh: ")
    p.sendline("0")
    p.expect("-> ray.util.pdb.set_trace()")
    p.sendline("c")

    ray.get([result1, result2])


def test_ray_debugger_stepping(shutdown_only):
    os.environ["RAY_DEBUG"] = "legacy"
    ray.init(num_cpus=1, runtime_env={"env_vars": {"RAY_DEBUG": "legacy"}})

    @ray.remote
    def g():
        return None

    @ray.remote
    def f():
        ray.util.pdb.set_trace()
        x = g.remote()
        return ray.get(x)

    result = f.remote()

    wait_for_condition(
        lambda: len(
            ray.experimental.internal_kv._internal_kv_list(
                "RAY_PDB_", namespace=ray_constants.KV_NAMESPACE_PDB
            )
        )
        > 0
    )

    p = _pexpect_spawn("ray debug")
    p.expect("Enter breakpoint index or press enter to refresh: ")
    p.sendline("0")
    p.expect("-> x = g.remote()")
    p.sendline("remote")
    p.expect("(Pdb)")
    p.sendline("get")
    p.expect("(Pdb)")
    p.sendline("continue")

    # This should succeed now!
    ray.get(result)


def test_ray_debugger_recursive(shutdown_only):
    os.environ["RAY_DEBUG"] = "legacy"
    ray.init(num_cpus=1, runtime_env={"env_vars": {"RAY_DEBUG": "legacy"}})

    @ray.remote
    def fact(n):
        if n < 1:
            return n
        ray.util.pdb.set_trace()
        n_id = fact.remote(n - 1)
        return n * ray.get(n_id)

    result = fact.remote(5)

    wait_for_condition(
        lambda: len(
            ray.experimental.internal_kv._internal_kv_list(
                "RAY_PDB_", namespace=ray_constants.KV_NAMESPACE_PDB
            )
        )
        > 0
    )

    p = _pexpect_spawn("ray debug")
    p.expect("Enter breakpoint index or press enter to refresh: ")
    p.sendline("0")
    p.expect("(Pdb)")
    p.sendline("remote")
    p.expect("(Pdb)")
    p.sendline("remote")
    p.expect("(Pdb)")
    p.sendline("remote")
    p.expect("(Pdb)")
    p.sendline("remote")
    p.expect("(Pdb)")
    p.sendline("remote")
    p.expect("(Pdb)")
    p.sendline("remote")

    ray.get(result)


def test_job_exit_cleanup(ray_start_regular):
    address = ray_start_regular["address"]

    driver_script = """
import time

import ray
ray.init(address="{}", runtime_env={{"env_vars": {{"RAY_DEBUG": "legacy"}}}})

@ray.remote
def f():
    ray.util.rpdb.set_trace()

f.remote()
# Give the remote function long enough to actually run.
time.sleep(5)
""".format(
        address
    )

    assert not len(
        ray.experimental.internal_kv._internal_kv_list(
            "RAY_PDB_", namespace=ray_constants.KV_NAMESPACE_PDB
        )
    )

    run_string_as_driver(driver_script)

    def one_active_session():
        return len(
            ray.experimental.internal_kv._internal_kv_list(
                "RAY_PDB_", namespace=ray_constants.KV_NAMESPACE_PDB
            )
        )

    wait_for_condition(one_active_session)

    # Start the debugger. This should clean up any existing sessions that
    # belong to dead jobs.
    _ = _pexpect_spawn("ray debug")

    def no_active_sessions():
        return not len(
            ray.experimental.internal_kv._internal_kv_list(
                "RAY_PDB_", namespace=ray_constants.KV_NAMESPACE_PDB
            )
        )

    wait_for_condition(no_active_sessions)


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows does not print '--address' on init"
)
@pytest.mark.parametrize("ray_debugger_external", [False, True])
def test_ray_debugger_public(shutdown_only, call_ray_stop_only, ray_debugger_external):
    redis_substring_prefix = "--address='"
    cmd = ["ray", "start", "--head", "--num-cpus=1"]
    if ray_debugger_external:
        cmd.append("--ray-debugger-external")
    out = ray._common.utils.decode(
        subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    )
    # Get the redis address from the output.
    redis_substring_prefix = "--address='"
    address_location = out.find(redis_substring_prefix) + len(redis_substring_prefix)
    address = out[address_location:]
    address = address.split("'")[0]

    ray.init(address=address, runtime_env={"env_vars": {"RAY_DEBUG": "legacy"}})

    @ray.remote
    def f():
        ray.util.pdb.set_trace()
        return 1

    result = f.remote()

    wait_for_condition(
        lambda: len(
            ray.experimental.internal_kv._internal_kv_list(
                "RAY_PDB_", namespace=ray_constants.KV_NAMESPACE_PDB
            )
        )
        > 0
    )

    active_sessions = ray.experimental.internal_kv._internal_kv_list(
        "RAY_PDB_", namespace=ray_constants.KV_NAMESPACE_PDB
    )
    assert len(active_sessions) == 1
    session = json.loads(
        ray.experimental.internal_kv._internal_kv_get(
            active_sessions[0], namespace=ray_constants.KV_NAMESPACE_PDB
        )
    )

    host, port = session["pdb_address"].split(":")
    if ray_debugger_external:
        assert host == services.get_node_ip_address(), host
    else:
        assert host == "localhost", host

    # Check that we can successfully connect to both breakpoints.
    tn = Telnet(host, int(port))
    tn.write(b"c\n")

    # The message above should cause this to return now.
    ray.get(result)


@pytest.mark.xfail(cluster_not_supported, reason="cluster not supported")
@pytest.mark.parametrize("ray_debugger_external", [False, True])
def test_ray_debugger_public_multi_node(shutdown_only, ray_debugger_external):
    c = Cluster(
        initialize_head=True,
        connect=False,
        head_node_args={
            "num_cpus": 0,
            "num_gpus": 1,
            "ray_debugger_external": ray_debugger_external,
        },
    )
    c.add_node(num_cpus=1, ray_debugger_external=ray_debugger_external)

    ray.init(runtime_env={"env_vars": {"RAY_DEBUG": "legacy"}})

    @ray.remote
    def f():
        ray.util.pdb.set_trace()
        return 1

    # num_gpus=1 forces the task onto the head node.
    head_node_result = f.options(num_cpus=0, num_gpus=1).remote()

    # num_cpus=1 forces the task onto the worker node.
    worker_node_result = f.options(num_cpus=1).remote()

    wait_for_condition(
        lambda: len(
            ray.experimental.internal_kv._internal_kv_list(
                "RAY_PDB_", namespace=ray_constants.KV_NAMESPACE_PDB
            )
        )
        == 2
    )

    active_sessions = ray.experimental.internal_kv._internal_kv_list(
        "RAY_PDB_", namespace=ray_constants.KV_NAMESPACE_PDB
    )
    assert len(active_sessions) == 2
    session1 = json.loads(
        ray.experimental.internal_kv._internal_kv_get(
            active_sessions[0], namespace=ray_constants.KV_NAMESPACE_PDB
        )
    )
    session2 = json.loads(
        ray.experimental.internal_kv._internal_kv_get(
            active_sessions[1], namespace=ray_constants.KV_NAMESPACE_PDB
        )
    )

    host1, port1 = session1["pdb_address"].split(":")
    if ray_debugger_external:
        assert host1 == services.get_node_ip_address(), host1
    else:
        assert host1 == "localhost", host1

    host2, port2 = session2["pdb_address"].split(":")
    if ray_debugger_external:
        assert host2 == services.get_node_ip_address(), host2
    else:
        assert host2 == "localhost", host2

    # Check that we can successfully connect to both breakpoints.
    tn1 = Telnet(host1, int(port1))
    tn1.write(b"c\n")

    tn2 = Telnet(host2, int(port2))
    tn2.write(b"c\n")

    # The messages above should cause these to return now.
    ray.get([head_node_result, worker_node_result])


def test_env_var_enables_ray_debugger():
    with unittest.mock.patch.dict(os.environ):
        os.environ["RAY_DEBUG_POST_MORTEM"] = "1"
        assert ray.util.pdb._is_ray_debugger_post_mortem_enabled(), (
            "Expected post-mortem Debugger to be enabled when "
            "RAY_DEBUG_POST_MORTEM env var is present."
        )

    with unittest.mock.patch.dict(os.environ):
        if "RAY_DEBUG_POST_MORTEM" in os.environ:
            del os.environ["RAY_DEBUG_POST_MORTEM"]

        assert not ray.util.pdb._is_ray_debugger_post_mortem_enabled(), (
            "Expected post-mortem Debugger to be disabled when "
            "RAY_DEBUG_POST_MORTEM env var is absent."
        )


if __name__ == "__main__":
    # Make subprocess happy in bazel.
    os.environ["LC_ALL"] = "en_US.UTF-8"
    os.environ["LANG"] = "en_US.UTF-8"
    sys.exit(pytest.main(["-sv", __file__]))
