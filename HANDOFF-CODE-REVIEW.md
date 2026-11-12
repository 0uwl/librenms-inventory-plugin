# Code review handoff — findings and suggested fixes

Findings from a full review of this repository (plugin source, both test suites,
fixtures, CI workflow, examples, packaging). Every "Bug" below was **confirmed by
executing the code**, not guessed. Suggested fixes are directional — verify against
the surrounding code and the DOCUMENTATION block, which documents intended behaviour
in detail and must be kept in sync with any behaviour change.

## Repo orientation

- Plugin: `inventory_plugins/librenms.py` (single file, ~1660 lines; DOCUMENTATION and
  EXAMPLES blocks at the top are user-facing contract — update them with any behaviour change).
- Unit tests: `tests/unit/test_librenms_inventory.py` (mocked HTTP via a route table
  keyed on URL suffixes; fixtures in `tests/unit/fixtures/`).
- Integration tests: `tests/integration/test_librenms_live.py` (skipped without
  `LIBRENMS_API`/`LIBRENMS_TOKEN`; do not expect them to run locally).
- README.md documents user-visible behaviour; several options have long prose there too.

## Verification commands (all must pass before you finish)

```bash
uv sync --locked
uv run pytest -v                                    # unit tests only, by design
uv run ruff check .
uv run yamllint .github/workflows examples/*.yml.dist
ANSIBLE_INVENTORY_PLUGINS=./inventory_plugins uv run ansible-doc -t inventory librenms
```

Baseline at review time: 141 unit tests passing, all lints clean, ansible-doc renders.

## Repo conventions to follow

- Error handling pattern: fail the run with `AnsibleError` naming the offending option
  and value for configuration mistakes; degrade gracefully with `self._problem(...)`
  (visible at `-v`) for per-device/per-group data problems. Match the surrounding style.
- The codebase is deliberately comment-heavy and docstring-heavy — follow that style;
  new behaviour needs a matching docstring and, if user-visible, DOCUMENTATION prose.
- Logging: `self._step(...)` for progress at `-vvvv`, `self._problem(...)` for
  degradation at `-v`. All messages prefixed `[librenms]` (the helpers add it).
- Tests: add fixtures under `tests/unit/fixtures/` and route them through the
  `DEFAULT_ROUTES`-style dict; one test per behaviour, descriptive test names.

---

## Bugs (fix first; each is a real crash or silent data loss)

### Bug 1 — Non-string `notes` field crashes the whole run

- Where: `inventory_plugins/librenms.py:915` (`_parse_notes`, called from
  `_set_host_variables` at ~line 858).
- Problem: `notes_value.splitlines()[0]` has no type guard. `_set_host_variables`
  only checks truthiness of `device.get("notes")`, so a numeric or otherwise
  non-string value from the API reaches it and raises
  `AttributeError: 'int' object has no attribute 'splitlines'`, killing the run.
- Confirmed: calling `plugin._parse_notes("host", 12345)` crashes.
- Suggested fix: mirror the guard `_parse_location` already has
  (`librenms.py:956`): if `not isinstance(notes_value, str)`, `_problem(...)`
  naming the device and return `None`.
- Test: fixture device with `"notes": 12345`; assert the run completes and no
  notes-derived vars are set.

### Bug 2 — Non-dict JSON response body crashes with an unhelpful traceback

- Where: `inventory_plugins/librenms.py:546` (`_parse_response`).
- Problem: a body that is valid JSON but not an object (e.g. `[1, 2, 3]`) passes the
  `payload is None` check and later dies with
  `AttributeError: 'list' object has no attribute 'get'` in `_get_devices`.
- Confirmed: routing `/devices` to a `RawResponse(b'[1, 2, 3]')` in the test harness
  crashes exactly that way.
- Suggested fix: after `json.loads`, if `payload is not None and not
  isinstance(payload, dict)`, raise `AnsibleError` naming the URL (same shape as the
  existing "is not JSON" error at ~line 575).
- Test: `RawResponse(b'[1, 2, 3]')` route; assert `AnsibleError` with a message
  naming the endpoint.

### Bug 3 — A single non-ASCII-only hostname kills the entire inventory run

- Where: `_ascii_hostname` (`inventory_plugins/librenms.py:782`) and
  `_derive_hostname` (`inventory_plugins/librenms.py:794`).
- Problem: `encode("ascii", "ignore")` collapses a hostname with no
  ASCII-representable characters (e.g. `中`) to `""`, then
  `self.inventory.add_host("")` at `librenms.py:1549` raises
  `AnsibleError: Invalid empty host name provided` — one odd device takes down
  inventory generation for everyone. (Note: `é` survives as `e` via NFKD; only
  non-decomposable scripts collapse.)
- Confirmed: fixture device with `sysName`/`hostname` of `中` crashes the run.
- Suggested fix: in `_derive_hostname`, treat an empty ASCII conversion result like
  the missing-field case — fall through to the remaining candidate fields, then the
  existing uuid fallback at `librenms.py:810` (with its `_problem` message).
- Test: fixture with a CJK-only sysName; assert the host is added under a generated
  name and the run completes.

### Bug 4 — Optional capture group that doesn't participate yields `family = None`

- Where: `inventory_plugins/librenms.py:1121` (`_trim_hardware`).
- Problem: `family = match.group(1) if pattern.groups else match.group(0)` returns
  `None` when the pattern's first group is optional and did not participate
  (e.g. pattern `^(WS-)?(C\d+)[A-Z]*` against `C9300-48P`). Then:
  - with `lowercase_hardware_family: true`: crash at `librenms.py:887`
    (`'NoneType' object has no attribute 'lower'`);
  - without it: `libre_hardware_family: null` is silently set.
- Confirmed: both branches reproduced by direct call.
- Suggested fix: `family = match.group(1) or match.group(0)`; consider whether the
  DOCUMENTATION (hardware_trimming_patterns block, ~line 245) needs a sentence that
  the first capture group must participate, or that a non-participating one falls
  back to the whole match.
- Test: pattern with an optional leading group, both values of
  `lowercase_hardware_family`.

### Bug 5 — Duplicate hostnames merge silently

- Where: `_populate`, `inventory_plugins/librenms.py:1549` (and `added_hosts` set).
- Problem: two devices reporting the same `sysName` produce one host whose vars are
  overwritten by whichever device is processed last, with no warning at any
  verbosity. Every other degenerate case in this plugin is reported at `-v`
  (generated hostnames, unmatched mapping keys, colliding group names), so this is
  an inconsistency, and a silent one on network gear where duplicates do happen.
- Suggested fix: before `add_host`, if `hostname in self.added_hosts`, `_problem(...)`
  naming both devices (use `self._device_label(device)`); still add vars only for the
  first (current behaviour) or document the winner — pick one and document it.
- Test: two fixture devices with the same sysName; assert a `-v` message and
  deterministic var outcome.

---

## Minor / hardening (fix after the bugs, or as a follow-up commit)

6. **`query_filters` appended raw** — `inventory_plugins/librenms.py:628`. A value
   containing a space, `&`, or `#` silently corrupts the query string. Either
   `urllib.parse.quote` the value side or document in DOCUMENTATION that values must
   be pre-encoded.
7. **KeyError risks in `_get_device_group_membership`** — `librenms.py:665` and
   `librenms.py:734` assume `group["name"]` and `device["device_id"]` exist.
   Everywhere else the plugin defends against API-shape surprises (blank names,
   non-string locations); use `.get()` + skip-with-`_problem` here too.
8. **No test for the documented `!vault` api_token path** — README and
   `examples/librenms.yml.dist` both show `api_token: !vault |`, but
   `TestApiTokenTemplating` only covers plain and Jinja2 tokens. Add a unit test
   encrypting a token with `ansible-vault` (or a pre-encrypted fixture) so a
   regression in decryption cannot ship unnoticed.
9. **`headers` option can silently override the auth token** — user headers are
   merged after `X-Auth-Token` at `librenms.py:1608`. Probably intentional; add one
   line to the `headers` DOCUMENTATION entry saying so.
10. **Integration test brittleness** — `tests/integration/test_librenms_live.py:74`
    uses private `ssl._create_unverified_context()` (public equivalent:
    `ssl.create_default_context()` with `check_hostname=False` and
    `verify_mode=ssl.CERT_NONE`); `:253` `assertNotIn("WARNING", result.stderr)`
    false-fails if any device/group name contains that word — assert on Ansible's
    actual warning prefix instead.
11. **`_current_umask()`** — `librenms.py:1514` briefly sets the process umask to 0
    (not thread-safe). Fine as-is for inventory plugins; add a one-line comment so a
    future refactor doesn't move it into a threaded path.
12. **Doc typos** — `README.md:46` "bellow" → "below";
    `inventory_plugins/librenms.py:349` (EXAMPLES block) "derrive" → "derive";
    `examples/constructed.yml.dist:21` "explaination" → "explanation".

## Things checked and deliberately NOT findings (do not "fix" these)

- `self.inventory.processed_sources` exists in ansible-core >= 2.18 (verified in the
  2.18 source), so the unpinned `ansible-core` dependency is safe for any
  Python-3.12-capable controller.
- Numeric/boolean/null-looking hostnames (`12345`, `true`, `null`) round-trip
  correctly through Ansible's yaml inventory plugin — it stringifies host keys.
- The `!unsafe` tagging logic, snapshot atomicity (temp file + fsync + `os.replace`
  + directory fsync), and the foreign-file overwrite guard are correct as written.
- Cache read/write/skip logic and `cache_force_update` behave as documented.

## Suggested commit structure

1. Bugs 1–3 (crash-on-bad-API-data class) + their tests — one commit.
2. Bugs 4–5 + tests — one commit.
3. Minor items 6–12 — one or two commits, typos can ride along with any of them.

Keep each behaviour change paired with its DOCUMENTATION/README update in the same
commit. Do not reformat or "clean up" unrelated code; the existing comment density is
intentional.
