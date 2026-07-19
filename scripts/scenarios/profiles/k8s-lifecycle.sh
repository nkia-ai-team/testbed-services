#!/usr/bin/env bash
exec python3 "$(dirname "$0")/k8s_lifecycle_executor.py" "$@"
