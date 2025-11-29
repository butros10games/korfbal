#!/bin/sh
set -e

# Find jemalloc and set LD_PRELOAD
JEMALLOC_PATH=$(find /usr/lib -name libjemalloc.so.2 -print -quit)

if [ -n "$JEMALLOC_PATH" ]; then
    export LD_PRELOAD="$JEMALLOC_PATH"
    echo "✅ jemalloc found at $JEMALLOC_PATH. LD_PRELOAD set."
else
    echo "⚠️  WARNING: libjemalloc.so.2 not found! Memory usage may be high."
fi

exec "$@"
