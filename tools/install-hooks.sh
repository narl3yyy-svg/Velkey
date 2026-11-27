#!/bin/sh
# Run the privacy check before every `git push`.
set -e
cd "$(git rev-parse --show-toplevel)"
hook="$(git rev-parse --git-path hooks)/pre-push"
cat > "$hook" <<'HOOK'
#!/bin/sh
exec python3 tools/privacy_check.py "$@"
HOOK
chmod +x "$hook"
list="$(git rev-parse --git-dir)/privacy-patterns"
[ -f "$list" ] || printf '# Words that must never be pushed (one per line). This file is not part of the repo.\n' > "$list"
echo "Installed. Add your private words to: $list"
