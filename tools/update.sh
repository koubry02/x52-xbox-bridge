#!/bin/bash
# update.sh — pull the latest code and deploy it, with a rollback if it breaks.
#
# Runs as its OWN systemd unit, never as a child of hotas-oberon. That is not
# a style choice: the service has no KillMode set, so systemd's default
# 'control-group' kills every process in its cgroup on restart — an updater
# spawned by the service would kill itself half way through. nohup and setsid
# do not help; they stay in the cgroup. A separate unit has its own.
#
#   update.sh check     fetch and report what is available. Changes nothing.
#   update.sh apply     validate, deploy, restart, verify, roll back on failure
#
# Progress is appended as one JSON object per line to update.jsonl, so the web
# page can read back everything that happened while it was disconnected during
# the restart.
set -u

MODE="${1:-apply}"
# Every path and name here is overridable so the whole thing — including the
# rollback — can be exercised against a throwaway tree instead of the real one.
# Nothing sets these in normal use.
STATE="${HOTAS_STATE:-/var/lib/hotas-bridge}"
SRC="$STATE/src"
LOG="$STATE/update.jsonl"
DST="${HOTAS_DST:-/opt/hotas-bridge}"
UNIT="${HOTAS_UNIT:-hotas-oberon}"
DEFAULT_REPO="https://github.com/koubry02/x52-xbox-bridge"

# Where to pull from. The web app writes update.json when you point the board
# at a fork; an environment variable still wins, and the built-in default is
# the last word. Anything the web app would not accept is ignored here too.
cfg() {
    python3 - "$STATE/update.json" "$1" <<'PYCFG' 2>/dev/null
import json, re, sys
try:
    v = str(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except Exception:
    sys.exit(0)
pat = (r"^https://[A-Za-z0-9][A-Za-z0-9.\-]*(:[0-9]{1,5})?"
       r"/[A-Za-z0-9][A-Za-z0-9._\-]*/[A-Za-z0-9][A-Za-z0-9._\-]*(\.git)?/?$"
       if sys.argv[2] == "repo" else r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,80}$")
if re.match(pat, v) and ".." not in v:
    print(v)
PYCFG
}
REPO="${HOTAS_REPO:-$(cfg repo)}"; REPO="${REPO:-$DEFAULT_REPO}"
BRANCH="${HOTAS_BRANCH:-$(cfg branch)}"; BRANCH="${BRANCH:-main}"
WEBPORT="${HOTAS_WEBPORT:-8088}"
XPORT="${HOTAS_XPORT:-26401}"
HEALTH_S="${HOTAS_HEALTH_S:-45}"

mkdir -p "$STATE" 2>/dev/null || true
chmod 700 "$STATE" 2>/dev/null || true

esc() { printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()), end="")'; }
say() {  # say <step> <state> <message>
    printf '{"t":%s,"step":"%s","state":"%s","msg":%s}\n' \
        "$(date +%s)" "$1" "$2" "$(esc "$3")" >> "$LOG"
}
finish() { say done "$1" "$2"; exit "${3:-0}"; }

# The web app truncates the log and writes the first line when it starts
# the unit, so a browser polling immediately never reads the last run's
# verdict. Started by hand instead? Then nobody has, so do it here.
grep -q '"step":"queued"' "$LOG" 2>/dev/null || : > "$LOG"
say start run "$([ "$MODE" = check ] && echo 'Checking for updates' || echo 'Starting update')"

# ── the clone we pull into (never the deployed copy) ──────────────────────
if [ ! -d "$SRC/.git" ]; then
    say fetch run "First run — cloning $REPO"
    rm -rf "$SRC"
    if ! timeout 120 git clone --quiet "$REPO" "$SRC" 2>/dev/null; then
        finish failed "Could not reach $REPO. Is the board online?" 1
    fi
fi

cd "$SRC" || finish failed "No source tree at $SRC" 1
git remote set-url origin "$REPO" 2>/dev/null || true

say fetch run "Fetching $BRANCH"
if ! timeout 120 git fetch --quiet origin "$BRANCH" 2>/dev/null; then
    finish failed "Could not reach the repository. Check the board's network." 1
fi

LOCAL=$(cat "$DST/VERSION" 2>/dev/null | head -1)
[ -z "${LOCAL:-}" ] && LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
    say fetch ok "Already on the latest version"
    finish uptodate "You are up to date."
fi

# What is actually landing — useful to see before it happens. Point the board
# at a fork and the running commit may be nothing this repository has ever
# heard of, so fall back to simply listing what is at the other end.
if git cat-file -e "${LOCAL}^{commit}" 2>/dev/null; then
    RANGE="$LOCAL..origin/$BRANCH"
    COUNT=$(git rev-list --count "$RANGE" 2>/dev/null || echo "?")
    say fetch ok "$COUNT new commit(s) available"
else
    RANGE="origin/$BRANCH"
    COUNT="?"
    say fetch ok "A different version is available (this board is not running a commit from $REPO)"
fi
git log --oneline --no-decorate "$RANGE" 2>/dev/null | head -20 |
    while IFS= read -r line; do say commit info "$line"; done

if [ "$MODE" = "check" ]; then
    finish available "$([ "$COUNT" = "?" ] && echo "An update" || echo "$COUNT update(s)") ready to install."
fi

# ── validate the NEW code before the running service is touched ───────────
say validate run "Checking the new version before installing it"
git reset --hard --quiet HEAD && git clean -qfd
git checkout --quiet "origin/$BRANCH" 2>/dev/null || finish failed "Could not check out $BRANCH" 1

if ! python3 -m py_compile oberon/*.py 2>/dev/null; then
    finish failed "The new version has a syntax error — not installing it." 1
fi
if ! python3 - <<'PYCHK' 2>/dev/null
import sys, os
sys.path.insert(0, "oberon")
import layouts as L
d = "layouts"
names = L.list_layouts(d)
assert names, "no layouts"
for n in names:
    probs = L.validate(d, n, L.load_raw(d, n))
    assert not probs, (n, probs)
    L.resolve(d, n)
PYCHK
then
    finish failed "The new version's layouts do not validate — not installing." 1
fi
if ! timeout 20 python3 oberon/oberon_server.py --help >/dev/null 2>&1; then
    finish failed "The new server would not start — not installing." 1
fi
say validate ok "New version looks sound"

# ── snapshot what works, so the rollback can never itself fail ────────────
# Rolling back by checking out the old commit and reinstalling sounds neater,
# and is worse: it assumes the running version is a commit this clone has. It
# often isn't — the first install is usually a copied folder. A tarball of the
# thing that is actually running has no such assumption.
say install run "Backing up the version that works"
if ! tar czf "$STATE/rollback.tgz" -C "$(dirname "$DST")" \
        --exclude='layouts-backup-*' "$(basename "$DST")" 2>/dev/null; then
    finish failed "Could not back up the current install — not touching it." 1
fi

# ── deploy, reusing the installer's backup + layout protection ────────────
say install run "Installing (your layouts are backed up first)"
if ! MFD=no DEPS=no bash "$SRC/install.sh" oberon >/dev/null 2>&1; then
    finish failed "Install step failed — the old version is still running." 1
fi
printf '%s\n' "$REMOTE" > "$DST/VERSION"
git log -1 --pretty=%s >> "$DST/VERSION"
say install ok "Files in place"

# ── restart and prove it actually came up ─────────────────────────────────
say restart run "Restarting the bridge"
systemctl restart "$UNIT" 2>/dev/null

healthy() {
    systemctl is-active --quiet "$UNIT" || return 1
    curl -sf -m 2 "http://127.0.0.1:$WEBPORT/" >/dev/null 2>&1 || return 1
    # the Oberon port must actually be listening, not just the web app
    timeout 2 bash -c "echo > /dev/tcp/127.0.0.1/$XPORT" 2>/dev/null || return 1
    return 0
}

for _ in $(seq 1 "$HEALTH_S"); do
    sleep 1
    healthy && { say restart ok "Bridge is back up"
                 finish updated "Updated and running the new version."; }
done

# ── it did not come up: put the old version back ──────────────────────────
say rollback run "The new version did not come up — putting the old one back"
# Without this systemd refuses to start a unit that has just crash-looped.
systemctl reset-failed "$UNIT" 2>/dev/null || true

# Unpack beside the install rather than over it: extracting on top would leave
# the new version's extra files sitting in the tree, which is how a "restored"
# install ends up as a mixture of two versions. Swap directories instead, and
# keep the broken one until the swap has actually succeeded.
BASE=$(basename "$DST"); PARENT=$(dirname "$DST")
STAGE="$PARENT/.hotas-rollback.$$"
rm -rf "$STAGE" "$DST.broken"
mkdir -p "$STAGE"
if tar xzf "$STATE/rollback.tgz" -C "$STAGE" 2>/dev/null && [ -d "$STAGE/$BASE" ]; then
    if mv "$DST" "$DST.broken" 2>/dev/null && mv "$STAGE/$BASE" "$DST" 2>/dev/null; then
        rm -rf "$DST.broken" "$STAGE"
        # /etc has the failed version's unit files; the good ones came back
        # inside the tree, so put them where systemd looks.
        for u in hotas-oberon.service hotas-update.service hotas-update-check.service; do
            f=$(find "$DST" -name "$u" 2>/dev/null | head -1)
            [ -n "${f:-}" ] && cp "$f" "${HOTAS_SYSTEMD:-/etc/systemd/system}"/"$u"
        done
        systemctl daemon-reload 2>/dev/null || true
        systemctl reset-failed "$UNIT" 2>/dev/null || true
        systemctl restart "$UNIT" 2>/dev/null
        for _ in $(seq 1 30); do
            sleep 1
            healthy && finish rolled-back \
                "That version would not run. The previous one is back and working." 1
        done
    else
        # The swap did not happen; leave what was there where it was.
        [ -d "$DST" ] || mv "$DST.broken" "$DST" 2>/dev/null || true
    fi
fi
rm -rf "$STAGE"
finish failed "Update failed AND rollback failed. Recover over SSH: see the README." 1
