#!/bin/sh
# pre-push -- run every CI check locally. Install with:
#   ln -s ../../utils/pre-push.sh .git/hooks/pre-push
set -e

root="$(git rev-parse --show-toplevel)"
cd "$root"
# shellcheck disable=SC1091
. .venv/bin/activate

upstream="$(git rev-parse --abbrev-ref '@{upstream}' 2>/dev/null || echo HEAD~1)"
if git diff --name-only --diff-filter=ACM "$upstream" HEAD 2>/dev/null | grep -q '^docs/[^/]*/'; then
    echo "ERROR: push contains files inside a docs/ subdirectory. Remove them before pushing."
    exit 1
fi

run() {
    echo "Running $*..."
    if ! "$@"; then
        echo ""
        echo "ERROR: '$*' failed. Fix it, commit, and push again."
        exit 1
    fi
}

run ruff check .
run ruff format --check .
run pylint saturday_serial/ tests/
run mypy saturday_serial/ tests/
run bandit -q -r saturday_serial/ -lll
run yamllint .
run shellcheck -s sh install.sh mister/user-startup.udev.sh mister/user-startup.watch.sh utils/pre-push.sh
run pytest -q

echo ""
echo "All checks passed."
