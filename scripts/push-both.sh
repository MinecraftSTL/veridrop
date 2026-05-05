#!/usr/bin/env bash
# Push current branch to BOTH the private working repo (full-check) AND
# the public OSS repo (public). Use this for OSS-safe commits that should
# end up in canarybyte/veridrop too.
#
# Usage:
#   ./scripts/push-both.sh           # pushes current branch to both
#   ./scripts/push-both.sh -n        # dry run (show what would push)
#
# Skip this script when committing private-only code (commission tracking,
# ad infra, anything that should NOT be open source). For those, use:
#   git push full-check <branch>   # private only
set -euo pipefail

# Resolve the branch we're on. Detached HEAD bails out — pushing a
# detached HEAD silently to a default branch elsewhere is a recipe for
# disasters.
branch=$(git symbolic-ref --short -q HEAD || true)
if [[ -z "$branch" ]]; then
  echo "✗ HEAD is detached. Check out a branch first." >&2
  exit 1
fi

# Required remotes — fail fast with a clear message if either is missing.
for remote in full-check public; do
  if ! git remote get-url "$remote" >/dev/null 2>&1; then
    echo "✗ remote '$remote' not configured." >&2
    echo "  Run: git remote add $remote git@host:owner/repo.git" >&2
    exit 1
  fi
done

dry_run=false
if [[ "${1:-}" == "-n" || "${1:-}" == "--dry-run" ]]; then
  dry_run=true
fi

push() {
  local remote="$1"
  local label="$2"
  echo
  echo "→ $label ($remote)"
  if $dry_run; then
    git push --dry-run "$remote" "$branch"
  else
    git push "$remote" "$branch"
  fi
}

push full-check "private (tuofangzhe/veridrop-full-check)"
push public     "public OSS (canarybyte/veridrop)"

echo
echo "✓ branch '$branch' synced to both remotes"
