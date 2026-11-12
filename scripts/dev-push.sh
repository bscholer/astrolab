#!/usr/bin/env bash
# Copy the working tree's code into the running astrolab pod for a quick look.
#
# The pod must run uvicorn with --reload, which the image does when /data/dev-reload
# exists. The first run creates that file and asks for one pod restart. After that, a push
# takes seconds: Python changes restart the API by themselves.
#
# This is for trying things. The next real deploy (push to main) replaces the pod, and
# with it everything copied here. Dependency changes (pyproject.toml) need a real build.
#
#   scripts/dev-push.sh            server, nodes, templates, profiles, catalogs
#   scripts/dev-push.sh --ui       also build the UI and copy it in
#   scripts/dev-push.sh --worker   also restart the job worker, for changes it runs
#                                  (this kills a pipeline job that is running)

set -euo pipefail

NS="${ASTROLAB_NAMESPACE:-astrolab}"
TARGET="${ASTROLAB_TARGET:-deploy/astrolab}"
ui=0
worker=0
for arg in "$@"; do
    case "$arg" in
        --ui) ui=1 ;;
        --worker) worker=1 ;;
        -h | --help)
            sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown option: $arg" >&2
            exit 2
            ;;
    esac
done

cd "$(git rev-parse --show-toplevel)"
k() { kubectl -n "$NS" "$@"; }
start=$(date +%s)

if ! k exec "$TARGET" -- sh -c 'tr "\0" " " </proc/1/cmdline | grep -q -- --reload'; then
    k exec "$TARGET" -- touch /data/dev-reload
    echo "The pod is not running with reload. /data/dev-reload is now set, so the next pod"
    echo "start turns it on. Restart the pod once (or wait for the next deploy), then rerun."
    exit 1
fi

if [[ "$ui" == 1 ]]; then
    echo "building the UI"
    (cd ui && npm run build --silent >/dev/null)
fi

echo "copying code into $NS/$TARGET"
tar -cf - --exclude=__pycache__ --exclude='*.pyc' server nodes templates profiles catalogs |
    k exec -i "$TARGET" -- tar -xf - -C /app
if [[ "$ui" == 1 ]]; then
    tar -C ui -cf - build | k exec -i "$TARGET" -- tar -xf - -C /app/ui
fi

if [[ "$worker" == 1 ]]; then
    echo "restarting the job worker"
    # Logs go to PID 1's stdout so they still show in kubectl logs.
    # Anchored, so the match skips this sh -c, whose command line also names the worker.
    k exec "$TARGET" -- sh -c "pkill -f '^python -m server.worker'; sleep 1
        cd /app && setsid -f python -m server.worker >/proc/1/fd/1 2>&1 </dev/null"
fi

# Give uvicorn's reloader a moment to notice, then wait until the API answers again.
sleep 2
for _ in $(seq 1 60); do
    if k exec "$TARGET" -- python -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=2)" \
        >/dev/null 2>&1; then
        echo "live in $(($(date +%s) - start))s"
        exit 0
    fi
    sleep 1
done
echo "the API did not come back. Recent logs:" >&2
k logs "$TARGET" --tail=40 >&2
exit 1
