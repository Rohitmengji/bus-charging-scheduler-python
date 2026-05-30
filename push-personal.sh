#!/usr/bin/env bash
# ============================================================================
# push-personal.sh — Push via feature branch + auto PR + optional auto-merge
# ============================================================================
# Usage:
#   ./push-personal.sh [commit message]
#
# Optional env overrides (recommended):
#   PERSONAL_NAME="Your Name"
#   PERSONAL_EMAIL="you@example.com"
#   GITHUB_USER="your-github-user"
#   REPO_NAME="your-repo"
#   REMOTE_NAME="personal"
#   BASE_BRANCH="main"
#
# Flow:
# 1) Sets repo-local git identity (safe: does not change global git config)
# 2) Ensures remote exists/updated
# 3) Stages + commits current changes
# 4) Pushes commit to a generated feature branch
# 5) Creates PR with GitHub CLI and enables auto-merge when possible
# ============================================================================

set -euo pipefail

print_help() {
  cat <<'EOF'
Usage:
  ./push-personal.sh [commit message]

Examples:
  ./push-personal.sh "feat: add scenario 6"
  PERSONAL_NAME="Jane" GITHUB_USER="jane" REPO_NAME="my-repo" ./push-personal.sh "chore: update"

Optional env vars:
  PERSONAL_NAME, PERSONAL_EMAIL, GITHUB_USER, REPO_NAME, REMOTE_NAME, BASE_BRANCH
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  print_help
  exit 0
fi

# ─── Defaults (override using env vars) ─────────────────────────────────────
PERSONAL_NAME="${PERSONAL_NAME:-Rohit Mengji}"
PERSONAL_EMAIL="${PERSONAL_EMAIL:-rohitmengjih@gmail.com}"
GITHUB_USER="${GITHUB_USER:-Rohitmengji}"
REMOTE_NAME="${REMOTE_NAME:-personal}"
BASE_BRANCH="${BASE_BRANCH:-main}"
COMMIT_MSG="${1:-feat: update bus charging scheduler}"
# ─────────────────────────────────────────────────────────────────────────────

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "${ROOT_DIR}" ]]; then
  echo "❌ Not inside a git repository."
  exit 1
fi
cd "${ROOT_DIR}"

# Auto-detect repo name from origin/personal remote if available, else folder name.
if git remote get-url origin >/dev/null 2>&1; then
  ORIGIN_URL="$(git remote get-url origin)"
  AUTO_REPO_NAME="$(basename -s .git "${ORIGIN_URL}")"
elif git remote get-url "${REMOTE_NAME}" >/dev/null 2>&1; then
  REMOTE_URL_EXISTING="$(git remote get-url "${REMOTE_NAME}")"
  AUTO_REPO_NAME="$(basename -s .git "${REMOTE_URL_EXISTING}")"
else
  AUTO_REPO_NAME="$(basename "${ROOT_DIR}")"
fi
REPO_NAME="${REPO_NAME:-${AUTO_REPO_NAME}}"

if ! command -v gh >/dev/null 2>&1; then
  echo "❌ GitHub CLI (gh) not found. Install: brew install gh"
  echo "   Then authenticate: gh auth login"
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "❌ gh is not authenticated. Run: gh auth login"
  exit 1
fi

# Generate a safe branch name from commit message.
BRANCH_NAME="$(echo "${COMMIT_MSG}" | sed 's/[^a-zA-Z0-9]/-/g' | sed 's/--*/-/g' | sed 's/^-//;s/-$//' | tr '[:upper:]' '[:lower:]' | cut -c1-50)"
BRANCH_NAME="${BRANCH_NAME:-update}"

echo "🔧 Setting repo-local git identity..."
git config user.name "${PERSONAL_NAME}"
git config user.email "${PERSONAL_EMAIL}"
git config pull.rebase true

echo "📧 Identity: $(git config user.name) <$(git config user.email)>"

REMOTE_URL="https://github.com/${GITHUB_USER}/${REPO_NAME}.git"
if git remote get-url "${REMOTE_NAME}" >/dev/null 2>&1; then
  git remote set-url "${REMOTE_NAME}" "${REMOTE_URL}"
else
  git remote add "${REMOTE_NAME}" "${REMOTE_URL}"
fi
echo "🔗 Remote '${REMOTE_NAME}' → ${REMOTE_URL}"

echo "🔄 Fetching ${REMOTE_NAME}/${BASE_BRANCH} (if available)..."
git fetch "${REMOTE_NAME}" "${BASE_BRANCH}" 2>/dev/null || true

echo "📝 Staging changes..."
git add -A

if git diff --cached --quiet; then
  echo "ℹ️ No staged changes to commit."
  exit 0
fi

echo "💾 Committing: ${COMMIT_MSG}"
git commit -m "${COMMIT_MSG}"

echo "🚀 Pushing to ${REMOTE_NAME}/${BRANCH_NAME}..."
git push "${REMOTE_NAME}" "HEAD:${BRANCH_NAME}"

echo "📋 Creating PR..."
PR_OUTPUT="$(gh pr create \
  --repo "${GITHUB_USER}/${REPO_NAME}" \
  --head "${BRANCH_NAME}" \
  --base "${BASE_BRANCH}" \
  --title "${COMMIT_MSG}" \
  --body "Auto-generated PR from push-personal.sh" \
  2>&1 || true)"

if echo "${PR_OUTPUT}" | grep -q "https://"; then
  PR_URL="$(echo "${PR_OUTPUT}" | grep -Eo 'https://[^ ]+' | head -n1)"
  echo "✅ PR created: ${PR_URL}"

  echo "🔀 Enabling auto-merge..."
  gh pr merge "${BRANCH_NAME}" \
    --repo "${GITHUB_USER}/${REPO_NAME}" \
    --squash \
    --auto \
    --delete-branch 2>/dev/null || echo "ℹ️ Auto-merge queued (or branch protection waiting for checks)."
else
  echo "⚠️ PR creation output:"
  echo "${PR_OUTPUT}"
fi

echo
echo "✅ Done! Repo: https://github.com/${GITHUB_USER}/${REPO_NAME}"
