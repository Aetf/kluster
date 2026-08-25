# Feedback backlog — 2026-08-25 review

Status: `todo` / `wip` / `done` / `discuss`(needs a decision from user first)

## Docs & tooling hygiene
1. [done] All markdown must pass ltex; extend `.vscode/ltex.dictionary.en-US.txt` (user seeded one entry).
2. [done] `state-dump.py` still trips ruff + pyright errors/warnings — fix.
3. [done] `state-dump.py` upload path mixes several request styles; `_request` exists but is not used everywhere. Refactor so the rule is explicit.
4. [done] `state-dump.py` has no unit tests.

## Credentials
5. [done] Use a Python KeePass library so `keepassxc-cli` stops being a dependency.
6. [todo] Add `kdbx init`: guided creation of the master keys from nothing — prompts for accounts, tokens, required permissions.
7. [todo] CLI needs real `--help` text and docs: which command runs when, and the expected overall order.
8. [done] kdbx entry naming/format rules are scattered across implementations; centralize.
9. [done] "root seed" is a misleading name — the seed layer holds many secrets, that is only one of them. Rename.
10. [done] CLI command actions are organized by protocol/account; they should follow the layering in the credentials doc. (Implementation staying account/protocol-organized is right.)
11. [done] Credentials is not only a script concept — the generic handling belongs in a higher layer that Pulumi also uses. The CLI should only be the kdbx front end.
12. [todo] kdbx now sits on the initial-provision path. The whole kluster initial provision (from zero or from an existing kdbx: mint seeds, provision state-backend, everything after) should be one scripted process, asking for the kdbx master password exactly once, resumable from any step / able to detect its own progress.
13. [done] Too much hand-rolled crypto in credentials. Prefer existing libraries even if that forces changing the algorithms.

## Physical / state-backend
14. [done] Things in `state-backend/settings.py` that renovate updates need verification.
15. [done] Physical stack has no logic for spreading A1 instances across multiple ADs — is that meant to live outside the stack?

## Renovate
16. [done] Split updates by domain: in-cluster (infra, apps) vs outside (talos etc), and expect-no-diff python packages separately. `deps-non-major` currently swallows everything.
17. [done] Renovate does not check `Pulumi.<stack>.yaml`, but the docs expect the talos version to be updated there.
18. [discuss] Does renovate update `skills-lock.json`?
22. [done] Does renovate check `mise.toml`, and should its `latest` pins become explicit versions?

## CI / stacks
19. [done] The apps stack needs ZeroTier only for possible split-horizon config. Find a design that drops that dependency — let an app that needs a split-horizon change touch both physical and apps, so the apps stack itself pays no ZT overhead.
20. [done] `preview.yml` path filters only match each stack's entrypoint; a change to `src/kluster/physical/*.py` matches no filter.

## Meta
21. [done] The external environment is documented nowhere and configured nowhere: the two GitHub repos, each repo's settings, which environments, which branch protections, which GitHub apps.
23. [done] Current status and working order are tracked in README — find a better home.
