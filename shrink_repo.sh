#!/bin/sh
# shrink_repo.sh - remove the storage that keeps the project zip at ~26 MB even after files are deleted.
#
# WHY IT STAYS BIG: the size is not your files, it is the git history in .git (about 20 MB of the 26 MB).
#  * .git holds every version of every file ever committed. Deleting a file from the working tree only
#    removes it from the NEXT commit; the old copy stays in history for ever.
#  * ~11 MB of that history is .agents/outputs (PDF page screenshots and page-text dumps written by the
#    coding agent) - they are in .gitignore now, but they were committed earlier, so history still has them.
#  * the rest is 80+ old copies of templates/index.html (about 0.4 MB each) stored loose, not delta-packed.
#  * .agents/outputs is also still on disk (11 MB) - it is a scratch folder, safe to delete.
# The app itself stores no uploaded PDFs: uploaded reports are read in memory. (Files waiting for approval
# on Settings > Review are held in the database only until you approve or discard them.)
#
# RESULT on a copy of your project: .git 20 MB -> ~1 MB, whole project zip 26 MB -> ~1.1 MB.
#
# USE: run from the project root (the folder that contains .git):   sh shrink_repo.sh
# It first makes a full backup at ../Financial-Intel-backup-<date>.tar.gz. It REWRITES git history, so if the
# project is connected to GitHub/Render you must force-push once afterwards (git push --force --all).
set -e
[ -d .git ] || { echo "Run this from the project root (no .git here)."; exit 1; }
BK="../Financial-Intel-backup-$(date +%Y%m%d%H%M).tar.gz"
tar czf "$BK" . && echo "Backup written: $BK"
echo "Before: $(du -sh .git | cut -f1) in .git"
# 1. take large generated files out of ALL history
git filter-branch -f --index-filter \
  'git rm -r -q --cached --ignore-unmatch .agents/outputs attached_assets "*.png" "*.pdf" "*.zip" "*.db" "*.sqlite" "*.sqlite3"' \
  -- --all >/dev/null 2>&1
# 2. drop the safety refs git keeps after a rewrite, then repack with delta compression
rm -rf .git/refs/original
git reflog expire --expire=now --all
git gc --aggressive --prune=now -q
# 3. delete the scratch folder from disk too
rm -rf .agents/outputs
echo "After:  $(du -sh .git | cut -f1) in .git ; project folder $(du -sh . | cut -f1)"
echo "If this project is on GitHub / Render: git push --force --all"
