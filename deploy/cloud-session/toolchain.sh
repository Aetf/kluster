#!/usr/bin/env bash
# The toolchain of a Claude Code cloud session: mise, every tool `mise.toml`
# pins, and the locked Python environment, so that AGENTS.md's gate runs there
# as written. docs/framework/dispatch.md §1.4 says where it runs from --
# a cloud environment's setup script, whose result the environment caches, and
# the `SessionStart` hook in `.claude/settings.json`, which runs it only in a
# cloud session -- and the network access its downloads need.
#
# Shell rather than a console script under `src/kluster/scripts/`, because it
# runs before the environment those scripts run in can exist: that environment
# is uv's, and uv is one of the tools this installs.
#
# Everything the installers print goes to a log under the clone's ignored
# `.claude/`, and success prints nothing: a `SessionStart` hook's stdout
# becomes context for the model. A failed step prints one line to stderr and
# exits non-zero, which fails a setup script and shows as a hook's error. Each
# step is a no-op when its work is already done, so the hook costs a session
# whose environment's setup script already ran only the checks.
#
# It reads no secret and writes nothing under `.credentials/`: a cloud session
# holds no credential (dispatch.md §1.4).
set -euo pipefail

root=$(cd "$(dirname "$0")/../.." && pwd)
scratch=$root/.claude
mkdir -p "$scratch"
log=$scratch/toolchain.log
: >"$log"

step() {
  "$@" >>"$log" 2>&1 || {
    echo "cloud toolchain: \`$*\` failed, output in $log." \
      "A refused download (403) means the cloud environment's network access" \
      "is not Full; that is set on claude.ai, not in the repository" \
      "(docs/framework/dispatch.md §1.4)." >&2
    exit 1
  }
}

# mise comes from npm, where its maintainers publish every release as a
# package holding the prebuilt binary: the npm registry is on a cloud
# environment's default network allowlist and Node is on its image, so this
# step needs nothing the environment's network setting decides. It installs
# under `~/.local` rather than npm's global prefix, which belongs to the
# image's Node. Nothing puts `~/.local/bin` on PATH -- not for this script, and
# not for the session's later commands, which take it from `CLAUDE_ENV_FILE`
# below when a hook runs this.
#
# The version is CI's, the `version` every workflow hands `jdx/mise-action`
# (framework/ci.md §3): the cloud gate runs the mise that CI runs, and renovate
# moves the two in one pull request. A mise already on PATH at another version
# is replaced. CI also sets `experimental: true`, which this script does not:
# nothing `mise.toml` declares resolves differently under it -- `mise env`,
# `tasks ls`, `ls --current` and `config ls` print the same either way.
mise_version=2026.10.1
bin=$HOME/.local/bin
export PATH=$bin:$PATH

if [ "$(mise --version 2>/dev/null | cut -d ' ' -f 1)" != "$mise_version" ]; then
  step npm install --global --prefix "$HOME/.local" "@jdxcode/mise@$mise_version"
fi

# The clone is the branch the session was given, so trusting its `mise.toml`
# is trusting that branch; `mise install` refuses an untrusted one.
cd "$root"
step mise trust
step mise install
step mise x uv -- uv sync --locked

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"$bin:\$PATH\"" >>"$CLAUDE_ENV_FILE"
fi
