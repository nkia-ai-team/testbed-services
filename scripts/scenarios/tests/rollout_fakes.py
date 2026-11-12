"""Fake kubectl and MockServer curl for the annotation-restart and mock timelines (timeline.compose,
timeline.multi rollout step, timeline_lock_mock recovery).

Not a test module. Deployments are JSON files ``deploy-<name>.json`` under FAKE_K8S; ``get deploy -o
json`` prints one and ``patch deploy --type=merge`` merges pod-template annotations into it (null
removes). ``port-forward``, ``rollout``, ``auth can-i`` succeed. curl answers MockServer liveness,
retrieve (FAKE_K8S/active.json), reset and expectation PUTs (logged). ``sleep`` returns at once.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

KUBECTL = r'''#!/usr/bin/env python3
import json, os, sys
root = os.environ["FAKE_K8S"]
args = sys.argv[1:]
with open(os.path.join(root, "calls"), "a") as log:
    log.write(" ".join(args) + "\n")
rest, i = [], 0
while i < len(args):
    if args[i].startswith("--kubeconfig="):
        i += 1
    elif args[i] in ("--kubeconfig", "-n"):
        i += 2
    else:
        rest.append(args[i]); i += 1
verb = rest[0]
if verb == "auth":
    print("yes"); sys.exit(0)
if verb in ("rollout", "port-forward"):
    sys.exit(0)
if verb == "get" and rest[1] in ("deploy", "deployment") and "json" in rest:
    print(open(os.path.join(root, "deploy-%s.json" % rest[2])).read()); sys.exit(0)
if verb == "patch" and rest[1] in ("deploy", "deployment"):
    path = os.path.join(root, "deploy-%s.json" % rest[2])
    doc = json.load(open(path))
    patch = json.loads(rest[rest.index("-p") + 1])
    annotations = doc["spec"]["template"]["metadata"].setdefault("annotations", {})
    for key, value in patch["spec"]["template"]["metadata"]["annotations"].items():
        if value is None:
            annotations.pop(key, None)
        else:
            annotations[key] = value
    json.dump(doc, open(path, "w")); sys.exit(0)
sys.stderr.write("unexpected kubectl %s\n" % " ".join(args)); sys.exit(9)
'''

CURL = r'''#!/usr/bin/env bash
url=""; for a in "$@"; do case "$a" in http://*) url="$a" ;; esac; done
case "$url" in
  *liveness*) exit 0 ;;
  *retrieve*) cat "$FAKE_K8S/active.json" 2>/dev/null || echo '[]' ;;
  *reset) echo '[]' >"$FAKE_K8S/active.json" ;;
  *expectation) printf '%s\n' "$*" >>"$FAKE_K8S/expectations.log" ;;
  *) echo "unexpected curl $*" >&2; exit 9 ;;
esac
'''


def install(root: Path, deployments: dict[str, dict[str, str] | None]) -> dict[str, str]:
    """Write the fakes and one Deployment file per name with the given pod-template annotations."""
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (("kubectl", KUBECTL), ("curl", CURL), ("sleep", "#!/bin/sh\nexit 0\n")):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    for name, annotations in deployments.items():
        metadata = {} if annotations is None else {"annotations": dict(annotations)}
        (root / f"deploy-{name}.json").write_text(json.dumps({"spec": {"template": {"metadata": metadata}}}))
    (root / "state").mkdir(exist_ok=True)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_K8S": str(root),
            "SCENARIO_PROFILE_STATE_ROOT": str(root / "state")}


def annotations(root: Path, name: str) -> dict[str, str]:
    doc = json.loads((root / f"deploy-{name}.json").read_text())
    return doc["spec"]["template"]["metadata"].get("annotations", {})
