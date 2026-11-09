"""A two-node overlay in user and network namespaces, for the network link executor.

Not a test module. The executor changes the egress qdisc of a node's overlay device, which only a
kernel can show, so the tests run it against a real one: an unprivileged user namespace owns two
network namespaces, "node" (the node the contract names) and "peer" (another node's pod network),
joined by a veth whose node end is called flannel.1 and has no transmit queue length, like the
VXLAN device on 66. Inside the user namespace the tests are root, so tc and ip work as on the node;
nothing outside the namespaces changes (the netem, prio and flower modules may be loaded on first use,
as any user's `unshare -rn tc ...` would).

The node side gets the cluster through fakes: ssh runs the remote command in the node namespace (and
fails while ``down`` exists), kubectl answers with the 66 node and pod layout.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

NODE_IP = "10.244.3.0"          # the node's overlay address (flannel.1): host-network pods leave from it
KCM_POD_IP = "10.244.3.49"      # a KCM pod on the node
APP_POD_IP = "10.244.3.69"      # an application pod on the node
PEER_IP = "10.244.1.56"         # a pod on another node
PEER_KCM_IP = "10.244.1.22"     # a KCM pod on the other node
PEER_NODE_IP = "10.244.1.0"     # the other node's overlay address (its host-network pods)

SSH = r'''#!/usr/bin/env bash
if [[ -e "$SANDBOX/down" ]]; then echo "ssh: connect to host: No route to host" >&2; exit 255; fi
while [[ "$1" == -* ]]; do [[ "$1" == -i || "$1" == -o ]] && shift; shift; done
shift
printf '%s\n' "$*" >>"$SANDBOX/ssh-log"
eval "set -- $*"
if [[ "$1" == sudo ]]; then shift; [[ "$1" == -n ]] && shift; fi
exec "$@"
'''

KUBECTL = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
def node(name, ip, cidr, ready="True"):
    return {"metadata": {"name": name}, "spec": {"podCIDR": cidr},
            "status": {"addresses": [{"type": "InternalIP", "address": ip}],
                       "conditions": [{"type": "Ready", "status": ready}]}}
def pod(ns, name, node, ip, ports, host=False):
    return {"metadata": {"namespace": ns, "name": name},
            "spec": {"nodeName": node, "hostNetwork": host,
                     "containers": [{"ports": [{"containerPort": p} for p in ports]}]},
            "status": {"podIP": ip}}
if a[-3:] == ["get", "nodes", "-o"] or "nodes" in a:
    ready = os.environ.get("FAKE_NODE_READY", "True")
    print(json.dumps({"items": [
        node("tb-cp", "192.168.122.77", "10.244.0.0/24"),
        node("tb-w1", "192.168.122.184", "10.244.3.0/24", ready),
        node("tb-w2", "192.168.122.11", "10.244.1.0/24"),
        node("tb-w3", "192.168.122.14", "10.244.2.0/24"),
    ]}))
elif "pods" in a:
    extra = [int(p) for p in os.environ.get("FAKE_KCM_PORTS", "").split(",") if p]
    print(json.dumps({"items": [
        pod("kcm-128", "kcm-node-agent-a", "tb-w1", "10.244.3.49", [18081]),
        pod("kcm-128", "kcm-node-agent-c", "tb-w2", "10.244.1.22", [18081]),
        pod("kcm-128", "kcm-master-agent-a", "tb-w3", "10.244.2.27", [4222, 18081] + extra),
        pod("kcm-monitoring", "kcm-node-agent-b", "tb-w1", "192.168.122.184", [18080], host=True),
        pod("rca-testbed-commerce", "testbed-payment-a", "tb-w1", "10.244.3.69", [8080]),
    ]}))
else:
    sys.exit(9)
'''

LISTENER = r'''
import socket, sys, threading, time
ip = sys.argv[1]
def serve(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((ip, port)); s.listen(64)
    while True:
        c, _ = s.accept(); c.close()
for port in map(int, sys.argv[2:]):
    threading.Thread(target=serve, args=(port,), daemon=True).start()
print("ready", flush=True)
time.sleep(600)
'''

CONNECT = r'''
import socket, sys, time
src, sport, ip, port = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
if src != "-" or sport:
    s.bind(("" if src == "-" else src, sport))
s.settimeout(10)
t = time.monotonic(); s.connect((ip, port)); took = time.monotonic() - t; s.close()
print(round(took * 1000, 1))
'''


class Sandbox:
    def __init__(self, tmp: Path, bin_dir: Path) -> None:
        self.tmp = tmp
        self.procs: list[subprocess.Popen] = []
        self.node = self._hold(["unshare", "-rn", "sleep", "600"])
        self.peer = self._hold(self._enter(self.node.pid) + ["unshare", "-n", "sleep", "600"],
                               f"/proc/{self.node.pid}/ns/net")
        node = [
            "ip link set lo up",
            f"ip link add flannel.1 type veth peer name eth9 netns {self.peer.pid}",
            "ip link set flannel.1 txqueuelen 0 up",
            f"ip addr add {NODE_IP}/32 dev flannel.1",
            f"ip addr add {KCM_POD_IP}/32 dev lo",
            f"ip addr add {APP_POD_IP}/32 dev lo",
            "ip route add 10.244.1.0/24 dev flannel.1",
            "ip route add 10.244.0.0/24 dev flannel.1",
        ]
        peer = [
            "ip link set lo up", "ip link set eth9 up", f"ip addr add {PEER_IP}/24 dev eth9",
            f"ip addr add {PEER_KCM_IP}/32 dev lo", f"ip addr add {PEER_NODE_IP}/32 dev lo",
            "ip route add 10.244.3.0/24 dev eth9",
        ]
        self.in_node(["bash", "-ec", "; ".join(node)], check=True)
        self.in_peer(["bash", "-ec", "; ".join(peer)], check=True)
        for name, body in (("ssh", SSH), ("kubectl", KUBECTL)):
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)
        (tmp / "ssh-log").touch()

    @staticmethod
    def _enter(pid: int) -> list[str]:
        return ["nsenter", "-t", str(pid), "-U", "-n", "--preserve-credentials"]

    def _hold(self, argv: list[str], outer: str = "/proc/self/ns/net") -> subprocess.Popen:
        """Start a namespace holder and wait until it has left the namespace it was started from."""
        before = os.readlink(outer)
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
        self.procs.append(proc)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if os.readlink(f"/proc/{proc.pid}/ns/net") != before:
                    return proc
            except OSError:
                pass
            time.sleep(0.02)
        raise RuntimeError("namespace did not come up")

    def in_node(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("timeout", 120)
        return subprocess.run(self._enter(self.node.pid) + argv, **kwargs)

    def in_peer(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("timeout", 60)
        return subprocess.run(self._enter(self.peer.pid) + argv, **kwargs)

    def listen(self, *ports: int, on_node: bool = False) -> None:
        """TCP listeners on the peer pod, or (on_node) on every address of the node."""
        where, ip = (self.node, "0.0.0.0") if on_node else (self.peer, PEER_IP)
        proc = subprocess.Popen(self._enter(where.pid) + ["python3", "-c", LISTENER, ip, *map(str, ports)],
                                stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
        self.procs.append(proc)
        assert proc.stdout is not None
        if proc.stdout.readline().strip() != b"ready":
            raise RuntimeError("listener did not start")

    def connect_ms(self, port: int, src: str = "-", sport: int = 0) -> float:
        """Milliseconds a TCP connect from the node to the peer pod takes (the SYN leaves through flannel.1)."""
        done = self.in_node(["python3", "-c", CONNECT, src, str(sport), PEER_IP, str(port)], check=True)
        return float(done.stdout)

    def connect_from_peer_ms(self, port: int, src: str) -> float:
        """Milliseconds a TCP connect from the other node to the node takes (the SYN-ACK leaves through flannel.1)."""
        done = self.in_peer(["python3", "-c", CONNECT, src, "0", NODE_IP, str(port)], check=True)
        return float(done.stdout)

    def root_qdisc(self) -> str:
        return self.in_node(["tc", "qdisc", "show", "dev", "flannel.1", "root"], check=True).stdout.decode()

    def close(self) -> None:
        for proc in reversed(self.procs):
            proc.kill()
            proc.wait(timeout=10)
            if proc.stdout is not None:
                proc.stdout.close()
