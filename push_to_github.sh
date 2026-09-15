#!/bin/bash
# One-shot setup: initializes git, stages everything, and commits --
# entirely on your machine, under your own git identity, so the commit
# is authored by you (not Claude). Run this from INSIDE the unzipped
# metric-sleuth folder:
#
#   cd metric-sleuth
#   bash push_to_github.sh
#
# Then follow the two remote/push commands it prints at the end.

set -e

# Safety check: refuse to run unless we're actually inside the right
# folder -- this is what went wrong before (running git commands one
# level up, in the parent folder).
if [ ! -f "README.md" ] || [ ! -d "gtm-metrics-explorer" ] || [ ! -d "gtm-intelligence-investigator" ]; then
  echo "ERROR: this doesn't look like the metric-sleuth project folder."
  echo "cd into the unzipped 'metric-sleuth' folder first, then re-run this script."
  exit 1
fi

echo "Working directory confirmed: $(pwd)"

# Clean up macOS/Python clutter that shouldn't be committed
find . -name ".DS_Store" -delete 2>/dev/null || true
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find . -name "*.pyc" -delete 2>/dev/null || true
rm -rf .pytest_cache 2>/dev/null || true

if [ -d ".git" ]; then
  echo "A .git folder already exists here -- reusing it rather than re-initializing."
else
  git init
fi

git add .
git reset -- push_to_github.sh > /dev/null 2>&1 || true   # this script itself doesn't belong in the repo

echo ""
echo "----- git status (review before continuing) -----"
git status
echo "---------------------------------------------------"
echo ""

git commit -m "metric-sleuth: two-layer GTM analytics -- workflow (Explorer) + agent (Investigator)"

echo ""
echo "Committed locally. Now run these two commands, with YOUR repo's URL"
echo "(copy it from the empty GitHub repo's page -- the 'quick setup' box):"
echo ""
echo "  git remote add origin <your-repo-url-here>"
echo "  git branch -M main"
echo "  git push -u origin main"
echo ""

# Clean up: remove this script from disk now that it's done its job --
# it was never part of the commit, so this just tidies your folder.
rm -- "$0"
