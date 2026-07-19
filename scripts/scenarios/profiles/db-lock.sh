#!/usr/bin/env bash
exec python3 "$(dirname "$0")/db_lock_executor.py" "$@"
