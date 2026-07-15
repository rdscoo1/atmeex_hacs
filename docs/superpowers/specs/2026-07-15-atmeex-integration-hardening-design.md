# Atmeex Cloud Integration Hardening Design

**Date:** 2026-07-15

**Status:** Approved

**Compatibility:** Home Assistant 2024.8 and newer
**User-facing contract:** Preserve existing entity unique IDs, entity/service names, service schemas, options, translations, and automation inputs

---

## Background

The integration has a modern asynchronous Home Assistant structure, but a repository-wide audit found correctness, lifecycle, privacy, performance, and quality-scale gaps. The most serious failures can make a cloud outage look successful, allow stale HTTP state to overwrite newer WebSocket state, prevent phone and WebSocket reauthentication, expose household data in diagnostics, and leave background work alive across unloads.

The existing tests are substantial but encode several faulty behaviors. The current local environment uses Home Assistant 2025.1.4 on Python 3.12, while the HACS manifest advertises Home Assistant 2024.8 as the minimum and current Home Assistant requires a newer Python runtime. The hardening work must therefore test both ends of the supported range explicitly.

This design fixes the root causes through focused internal primitives while keeping the public integration surface stable.

## Goals

1. Make API, authentication, coordinator, WebSocket, and task failures explicit and recoverable.
2. Guarantee that older poll or refresh responses cannot overwrite newer field values.
3. Serialize every logical device command atomically and preserve the most recent optimistic state.
4. Ensure setup and unload own the complete socket/task graph transactionally.
5. Distinguish a valid empty account from an unavailable or malformed cloud response.
6. Prevent diagnostics and support logs from exposing credentials or household identity.
7. Reduce request amplification, event-loop work, recorder churn, and logbook noise.
8. Preserve Home Assistant 2024.8 compatibility while following current Home Assistant behavior where the APIs differ.
9. Raise source coverage above 95% for every integration module and eliminate runtime/deprecation warnings.
10. Preserve the complete documented user-facing entity, service, option, and automation contract.

## Non-goals

- Removing or renaming existing entities, unique IDs, services, options, or event types.
- Adding new device features or changing Atmeex device semantics.
- Replacing the Atmeex cloud service or introducing local-device communication.
- Rewriting the integration around an external Python package.
- Making live Atmeex credentials or live cloud access a CI requirement.
- Claiming an official Home Assistant Integration Quality Scale rating for a custom integration.

## Constraints and compatibility policy

- Home Assistant 2024.8 remains the minimum supported release.
- Current Home Assistant is supported and tested on its required Python version.
- Compatibility behavior is isolated behind feature detection; production code does not scatter version-string comparisons.
- Existing installations keep registry choices. An entity that becomes disabled by default is disabled only when newly created; an existing enabled entity stays enabled.
- Existing diagnostic attributes and event identifiers remain readable. Churn is reduced by suppressing unchanged writes and technical logbook rendering rather than deleting the contract.
- Internal APIs may change freely when all in-repository call sites and tests migrate in the same plan.

## Delivery architecture

The work is divided into six independently testable and shippable subprojects. They execute in dependency order:

1. **API and authentication reliability**
2. **Versioned state convergence**
3. **Atomic command execution**
4. **WebSocket and runtime lifecycle**
5. **Coordinator, inventory, and availability**
6. **Home Assistant contract, privacy, performance, and quality gates**

Each subproject receives its own implementation plan. Every task follows red-green-refactor, ends with an atomic commit, and leaves the complete suite green.

## File responsibility map

### New production modules

#### `custom_components/atmeex_cloud/state_store.py`

Owns the canonical copy-on-write device snapshot and per-field revisions. It is the only component allowed to merge poll, targeted-refresh, or WebSocket state. Its public snapshot retains the current `AtmeexCoordinatorData` keys: `devices`, `device_map`, and `states`.

#### `custom_components/atmeex_cloud/command_executor.py`

Owns per-device command serialization, command generations, optimistic pending values, compound operations, cancellation cleanup, and the single post-command targeted refresh.

#### `custom_components/atmeex_cloud/compat.py`

Provides the small Home Assistant compatibility surface needed across 2024.8 and current releases: options-flow entry access, explicit update-and-reload behavior, and task creation helpers where signatures differ. Selection is based on available attributes/imports and is covered by both CI environments.

### Existing production modules

#### `custom_components/atmeex_cloud/api.py`

Remains the HTTP transport and token owner. It gains typed error classes, response-body expectations, generation-aware 401 recovery, safe refresh-token persistence callbacks, explicit auth timeouts, retry classification, strict payload validation, and normalized IDs/booleans.

#### `custom_components/atmeex_cloud/runtime.py`

Becomes the typed ownership root for API, coordinator, state store, command executor, WebSocket manager, stopping state, and every entry-owned task. Pending-command storage moves behind the command executor but compatibility accessors may remain temporarily for existing entity tests during migration.

#### `custom_components/atmeex_cloud/coordinator.py`

Owns authoritative inventory scheduling, availability transitions, bounded detail hydration, inventory-age enforcement, and publication of state-store snapshots. It no longer implements ad-hoc timestamp merge rules.

#### `custom_components/atmeex_cloud/__init__.py`

Remains the composition root. It constructs runtime data before starting background work, wires refresh-token persistence, forwards platforms transactionally, starts WebSocket processing after setup succeeds, and performs ordered unload cleanup.

#### `custom_components/atmeex_cloud/websocket.py`

Owns WebSocket transport only: connection, explicit token-recovery attempts, separate transport/auth counters, message validation, deterministic socket closure, and reconnect backoff. State mutation remains outside this module.

#### `custom_components/atmeex_cloud/entity_base.py`

Delegates commands to `AtmeexCommandExecutor`, combines coordinator availability with device connectivity, and converts internal errors into translated Home Assistant exceptions.

#### Platform modules

`climate.py`, `fan.py`, `select.py`, and `switch.py` express logical command payloads but do not acquire locks or call refreshes directly. All platform modules declare `PARALLEL_UPDATES`. Sensor and binary-sensor construction becomes capability-driven and avoids rebuilding known entity objects on every update.

#### Configuration and support modules

`config_flow.py` uses the compatibility layer and real flow-manager semantics. `diagnostics.py` exports an explicit safe schema. Routine device-update events remain on the Home Assistant bus for automations but are not registered as logbook-described events; meaningful recovery transitions add one explicit privacy-safe logbook entry. The manifest, translations, services, README files, HACS metadata, and CI workflows document and enforce the final contract.

## State convergence design

### Canonical identifiers and values

At the API boundary, every device receives a canonical string key through one helper. Outward device objects retain the representation required to keep existing unique IDs unchanged, but raw payload dictionaries are overwritten with the normalized ID instead of preserving a conflicting string/integer variant.

Boolean parsing uses explicit accepted literals:

- True: `True`, `1`, `"1"`, `"true"`, `"on"`, `"yes"`
- False: `False`, `0`, `"0"`, `"false"`, `"off"`, `"no"`, empty string

Unknown non-empty literals are protocol errors for authoritative HTTP data and are ignored for an isolated WebSocket field delta so the previous value survives.

### Per-field revisions

`AtmeexStateStore` maintains a monotonically increasing revision and a revision for each `(device_id, field)` pair. Network operations capture the relevant field revisions before I/O begins.

- A WebSocket delta applies only fields present in the message and advances those field revisions.
- A targeted refresh compares every returned field with its captured starting revision. It applies a field only when that field has not changed since the GET started.
- A full poll uses the same field-level rule. Fresh poll fields can land while a newer WebSocket value for another field remains intact.
- Device metadata uses the same merge rule instead of allowing an older detail response to replace newer inventory metadata.

This replaces the existing whole-device timestamps, which can either regress state or discard unrelated fresh fields.

### Authoritative inventory

Only a successful, schema-valid `/devices` response is authoritative. A valid empty list means the account currently has no devices. Network errors, authentication errors, rate limits, malformed collection shapes, and non-empty collections with no valid devices are failures and never become empty snapshots.

A device is considered removed after it is absent from two consecutive successful authoritative inventories. The two-success grace period protects against a transient partial cloud response without retaining removed devices forever. Failure cycles do not advance the absence count.

### Publication and notification

The store returns immutable-by-convention copy-on-write snapshots in the existing coordinator data shape. One logical mutation produces one coordinator publication. WebSocket messages accumulated during one event-loop turn are merged by device and field before publication.

The coordinator uses comparable state snapshots with `always_update=False`. Diagnostic timing data remains on coordinator properties rather than forcing the device snapshot unequal on every successful poll.

## Command execution design

### Command contract

Entities pass the executor:

- Device ID
- A callable that creates and performs the complete logical API operation after lock acquisition
- A mapping of expected pending fields and values
- Translation keys/placeholders for validation and execution errors

The callable factory prevents un-awaited coroutine objects when a caller is cancelled before acquiring the lock.

### Generations and ordering

Every command receives a monotonically increasing generation. Pending entries store the generation with the expected value. An older command may clear only pending entries carrying its own generation; it cannot erase a newer optimistic value.

The per-device `asyncio.Lock` covers the complete logical action, including every required write and its one final refresh. Different devices remain concurrent. Compound operations use one multi-field API payload whenever the Atmeex endpoint accepts it. If the protocol requires multiple requests, they still execute under one lock.

### Success, failure, and cancellation

- An API write failure clears only that command's pending generation and raises a translated `HomeAssistantError`.
- Invalid user input or unsupported capability raises a translated `ServiceValidationError`; it never silently returns.
- If the write succeeds but the confirmation GET fails, the action remains successful. Pending state stays until its bounded expiry or a later authoritative update, and an immediate coordinator recovery refresh is scheduled. This avoids encouraging users to repeat a command that the device may already have applied.
- If a multi-request operation partially succeeds and then fails, the executor schedules an authoritative refresh before surfacing the translated error.
- Cancellation clears only the cancelled command's still-current pending entries. Shared targeted refresh work is shielded so one waiter cannot cancel the owner task.

## API and authentication design

### Error model

The API exposes internal typed failures:

- `AtmeexAuthenticationError`
- `AtmeexConnectionError`
- `AtmeexRateLimitError`
- `AtmeexProtocolError`

Each carries a sanitized operation name and optional status/retry delay. Raw response bodies are not included in messages that can reach diagnostics or logs.

The coordinator maps authentication errors to `ConfigEntryAuthFailed`, transient communication/rate/protocol errors to `UpdateFailed`, and first-refresh transient failures to Home Assistant's normal `ConfigEntryNotReady` behavior.

### Request and retry policy

- Idempotent GET requests use at most three attempts with bounded backoff and an explicit 20-second timeout per attempt.
- Device-changing writes are never retried after ambiguous transport failure.
- SMS sends and one-time-code exchanges are never retried without a server-supported idempotency key.
- Authentication and refresh POSTs have explicit overall timeouts.
- Successful writes can declare `expect_json=False`; 204, empty, or text success bodies do not turn an applied command into a failure.
- A 429 preserves `Retry-After` when supplied and never destroys credentials.
- The HTTP `User-Agent` identifies this Home Assistant integration and its version; it does not impersonate the official Atmeex mobile application.

The duplicate primary/fallback call to the same `/devices` endpoint is removed. Alternate response shapes are parsed from one successful response rather than fetched again.

### Access-token generation

Each outgoing authenticated request captures the exact token and its generation before building headers. On 401/403, recovery acquires the existing token lock and follows this order:

1. If another caller already installed a newer valid generation, retry once with it.
2. If a refresh token exists, exchange it and retry once.
3. If email/password credentials exist, sign in and retry once.
4. Otherwise raise `AtmeexAuthenticationError` with the original authentication status.

The first response context is fully read/released before authentication and retry begins, avoiding nested connector usage.

### Refresh-token lifecycle

Refresh tokens are cleared only after a definitive invalid-token 401/403 from the refresh endpoint. Network failures, rate limits, malformed temporary responses, and 5xx responses retain the credential.

When a successful response rotates the refresh token, the API invokes a persistence callback supplied by the integration setup. The callback updates config-entry data without reloading the entry. Runtime token state is already current, so persistence is durable storage rather than a trigger for reinitialization.

Phone accounts use refresh-token recovery before any manual SMS reauthentication. SMS codes are never replayed automatically.

## WebSocket and lifecycle design

### Authentication and reconnects

Transport backoff and application authentication failures use separate counters. Successful TCP/WebSocket upgrade resets transport backoff only. Application unauthorized count survives reconnects and resets only after a validated Atmeex condition/settings message is accepted.

A handshake 401/403 receives one serialized token-recovery attempt. A repeated rejection or the configured consecutive application-unauthorized threshold invokes Home Assistant reauthentication exactly once and stops reconnecting until the entry is reloaded.

Reconnect ownership is explicit: a connection that closes immediately after a successful handshake schedules the next reconnect even if the previous reconnect wrapper is still finishing. The reconnect task reference is cleared only after the listener has either become stable or transferred ownership to its successor.

### Message buffering

Messages arriving during one event-loop turn are coalesced by device and field. Later values for the same field win within the batch. The queue remains bounded, but overflow is detected before eviction, increments a diagnostic counter, and schedules an immediate authoritative refresh.

The callback refuses to create new drain work after runtime enters the stopping state.

### Task and socket ownership

`AtmeexRuntimeData` tracks every entry-owned task in a set and removes completed tasks through callbacks. Tasks are created through the Home Assistant compatibility helper, not raw untracked `asyncio.create_task` calls.

Setup order:

1. Create API and token-persistence callback.
2. Perform the first authoritative coordinator refresh.
3. Construct state store, command executor, and complete runtime data.
4. Assign `entry.runtime_data`.
5. Forward platforms.
6. Start WebSocket and inventory-age tasks only after platform setup succeeds.

If platform forwarding or later setup fails, the runtime cleanup routine closes sockets, cancels/awaits tasks, and clears runtime data before re-raising.

Unload order:

1. Ask Home Assistant to unload platforms while runtime remains usable.
2. If platform unload fails, leave communications running and return `False`.
3. If it succeeds, mark runtime stopping.
4. Detach callbacks and close the captured active socket.
5. Cancel and await producer, consumer, refresh, reconnect, and watchdog tasks with bounded waits.
6. Clear runtime data and return `True`.

The WebSocket manager captures the socket locally and closes that exact object before listener cleanup can replace `self._ws` with `None`.

## Coordinator, availability, and performance design

### Coordinator failure semantics

An exhausted device-list failure raises; it never advances `last_success_ts` or clears `last_api_error`. Coordinator recovery uses Home Assistant's normal one-time unavailable/available transition logging.

Entity availability is:

```text
coordinator available AND device still present AND device reports online
```

The online binary sensor also becomes unavailable when coordinator data is unavailable; it does not indefinitely display stale connectivity.

### Detail hydration

The list response is used directly when it contains all state fields required by the integration. Devices missing required detail fields are hydrated with a bounded concurrency of three. One failed device detail request preserves that device's last confirmed detail only when the authoritative list itself succeeded; authentication failures still abort the entire refresh.

This changes healthy polling from unconditional sequential N+1 requests to one list request plus only necessary, bounded detail requests.

### Poll scheduling

Push notifications cannot be allowed to defer inventory discovery forever. Runtime maintains a maximum-inventory-age deadline based on the configured update interval. WebSocket publications do not move that deadline. When the deadline expires, the coordinator requests an authoritative inventory refresh even during continuous push traffic.

### Dynamic entities and recorder work

Each platform tracks known device/capability keys before constructing entities. Coordinator updates instantiate entities only for new keys.

Optional CO2 and other capability-specific sensors are created only when the individual device advertises or reports that capability. The account-wide option continues to control CO2 exposure, but it no longer creates unsupported `unknown` entities for every device.

Existing diagnostic attributes remain available, but unchanged device snapshots do not notify listeners. Timing attributes update when another meaningful state publication occurs rather than forcing a write by themselves. The diagnostics entity is disabled by default only for newly created registry entries.

Existing event identifiers and payload keys remain available for automations. Routine technical updates are removed from custom logbook rendering and more strongly coalesced/throttled; meaningful command, unavailable, and recovery transitions remain visible.

## Home Assistant compatibility and configuration design

### Options and reload behavior

The custom property that reads private `_config_entry` is removed. `compat.py` supplies a stable options-flow entry contract for Home Assistant 2024.8 and current flow managers, verified by initializing the flow through `hass.config_entries.options.async_init` in both CI jobs.

The generic config-entry update listener is removed. Options and reauthentication flows explicitly perform one update-and-reload through the compatibility helper. Internal refresh-token persistence performs an update without reload. This avoids both the current double-reload deprecation and reload loops after token rotation.

Email and normalized-phone unique IDs are checked before sign-in, device fetches, or SMS sends. A phone setup or reauthentication flow creates/updates an entry only when the verified login response supplies a usable refresh token; otherwise it reports an authentication/protocol error in the form instead of creating an entry that fails during setup.

### Reconfigure flow

A reconfigure flow allows proactive email/password or phone/SMS credential replacement. It validates credentials and confirms that the resulting account identity matches the existing unique ID before updating the entry. It never creates a second entry or silently switches an existing entry to another account.

### Services and platform behavior

Existing `set_breezer_mode` and `set_humidifier_stage` service names, target selectors, fields, and accepted values remain intact. They register once through Home Assistant's supported `EntityPlatform.async_register_entity_service` helper. That helper preserves target permissions, call context, entity serialization, feature/availability filtering, and polling behavior; integration code does not manually extract targets or call entity methods. Native entity actions continue to work.

All platforms define an explicit `PARALLEL_UPDATES`. Command platforms use `0` because the integration owns per-device serialization; coordinator-only platforms also use `0` because they perform no direct polling calls.

Exception messages use translation keys in `strings.json`, `translations/en.json`, and `translations/ru.json`.

### Device registry behavior

After the two-success absence threshold, the integration removes its config-entry association from a confirmed stale device. `async_remove_config_entry_device` returns `False` while any Atmeex identifier is still present in the authoritative inventory and `True` only for an absent device.

### Diagnostics and logging

Diagnostics are built from a whitelist, not from recursively redacted raw objects. Safe output includes integration version, non-identifying configuration flags, poll interval, WebSocket status, sanitized counters, coarse timing/latency, entity/device counts, and capability booleans.

Diagnostics omit or anonymize:

- Phone numbers, email addresses, passwords, access tokens, and refresh tokens
- Entry titles derived from credentials
- Device/user-provided names
- Internal/cloud IDs, registry identifiers, and area IDs
- Raw API or WebSocket payloads
- Raw server error bodies
- Coordinates or future unknown cloud fields

Debug logs use operation names, message types, counts, and anonymized stable-within-run device labels. They never emit complete condition/settings/device payloads.

### Metadata, typing, and documentation

- Remove `aiohttp` from manifest requirements because Home Assistant Core supplies it.
- Keep the HACS minimum at Home Assistant 2024.8.
- Introduce a typed config-entry alias and concrete runtime field types without importing newer-only Home Assistant types at runtime on 2024.8.
- Remove the tracked `.coverage` runtime artifact and ignore coverage databases/caches so verification does not dirty the repository.
- Document data update behavior, availability, services, options, supported devices/capabilities, removal, troubleshooting, limitations, and diagnostic privacy in both README languages.
- Keep current entity/service examples working and add migration-free upgrade notes.

## Testing strategy

Every implementation task follows TDD:

1. Add a focused failing regression test.
2. Run it and confirm it fails for the audited reason.
3. Implement the smallest production change.
4. Run the focused test until green.
5. Run the affected subsystem tests.
6. Run the complete suite.
7. Commit the self-contained change.

Concurrency tests use `asyncio.Event` barriers and controlled futures, never correctness assertions based on arbitrary sleeps.

### Required test layers

#### Pure unit tests

- Token generation and serialized recovery
- Response-body expectations and typed error mapping
- ID and boolean normalization
- State-store field revisions and authoritative inventory rules
- Command generations, pending cleanup, and cancellation

#### API contract tests

- Valid empty inventory versus transport/protocol failure
- Malformed collection shapes and invalid items
- 204/empty successful writes
- Retry/no-retry classification and explicit timeouts
- Phone refresh, definitive invalidation, rotation persistence, and SMS non-replay

#### Deterministic race tests

- Targeted GET starts, newer WebSocket delta lands, stale GET completes
- Poll returns unrelated fresh fields while preserving newer WebSocket fields
- Concurrent compound fan/climate/select/switch commands preserve later intent
- Older command failure cannot clear newer pending state
- Cancelled refresh waiter cannot cancel shared owner work

#### Lifecycle and stress tests

- Platform-forward failure performs full rollback
- Platform unload returning `False` leaves communications operational
- Active socket closes during blocked receive
- Immediate post-handshake close transfers reconnect ownership and reconnects again
- Unload during WebSocket and refresh activity leaves zero tasks and post-unload writes
- More than 500 partial messages trigger one resync and converge correctly
- Burst coalescing produces a bounded number of coordinator notifications

#### Home Assistant integration tests

- Options flow through the real flow manager on both supported environments
- Option update reloads exactly once
- Refresh-token persistence does not reload
- Reauth and reconfigure preserve account identity
- Coordinator failure marks entities unavailable and recovery restores them
- Confirmed stale devices are removable; active devices are not
- Existing entity IDs, services, schemas, options, and translation keys match compatibility snapshots

#### Privacy tests

Sentinel values for phone, email, credentials, entry title, device name, IDs, area, raw nested payloads, and server errors are inserted into every diagnostics source. The serialized diagnostics and captured logs must contain none of them.

## CI and release gates

The workflow contains two deliberate compatibility jobs:

1. Home Assistant 2024.8.x on Python 3.12 with a pinned compatible test stack.
2. Current Home Assistant on Python 3.14 with a separately pinned compatible test stack.

Both jobs run the complete suite. Shared gates require:

```text
0 test failures
0 runtime warnings
0 deprecation warnings owned by this repository
more than 95% source coverage for every integration module
compileall success
hassfest success
HACS validation success
```

Performance regression tests additionally assert:

- At most three concurrent detail requests
- No detail request when list data is complete
- One final refresh per logical compound action
- One state publication per coalesced WebSocket batch
- Immediate authoritative refresh after buffer overflow
- Inventory polling occurs within its configured maximum age during continuous WebSocket traffic

No subproject is considered complete until its focused tests and the full local suite pass. The sixth plan runs the two-environment matrix and final compatibility snapshots before release.

## Rollout and migration

The six plans merge in order. Each plan leaves production behavior deployable and carries its own regression tests. Internal compatibility adapters may exist temporarily between plans, but the final plan removes unused internal shims after both version-matrix jobs prove they are unnecessary.

No config-entry migration or entity-registry migration is expected because public identifiers and stored option keys remain unchanged. Rotated refresh-token persistence only updates existing config-entry data. Newly disabled-by-default diagnostics respect existing registry enablement choices.

## Audit traceability

| Audited problem | Owning subproject |
|---|---|
| Options flow accesses private `_config_entry` | 6 |
| Device-list outages become successful empty updates | 1, 5 |
| Entity availability ignores coordinator health | 5 |
| Targeted refresh overwrites newer WebSocket state | 2 |
| Poll preservation discards unrelated fresh fields | 2 |
| Compound commands release locks between steps | 3 |
| Select and climate paths bypass serialization | 3 |
| Pending cleanup is not generation/cancellation safe | 3 |
| Shared refresh waiters cancel owner work | 3, 4 |
| WebSocket unauthorized threshold resets on handshake | 4 |
| WebSocket handshake bypasses token recovery | 1, 4 |
| Phone refresh loses auth status or credential | 1 |
| Reactive 401 ignores phone refresh token | 1 |
| Rotated refresh token is not persisted | 1, 6 |
| Nested 401 retry retains the first response context | 1 |
| SMS/one-time auth retries and long default timeouts | 1 |
| 204/empty writes are reported as failures | 1 |
| Malformed collections become empty success | 1 |
| String booleans and mixed ID types corrupt state | 1, 2 |
| HTTP client impersonates the official mobile app | 1, 6 |
| Diagnostics expose phone, names, identifiers, areas, raw data | 6 |
| Raw cloud payloads appear in debug logs | 1, 4, 6 |
| Removed devices persist forever | 5 |
| Active devices are always manually removable | 5 |
| WebSocket disconnect can leak its socket | 4 |
| Setup/unload do not own the complete task graph | 4 |
| Sequential unconditional N+1 hydration | 5 |
| Push updates can starve inventory polling | 4, 5 |
| Queue overflow silently drops partial deltas | 4 |
| Immediate listener closure can lose reconnect ownership | 4 |
| Per-message notifications rebuild all entities | 4, 5 |
| Account-wide CO2 option creates unsupported entities | 5, 6 |
| Diagnostic attributes and logbook events create churn | 6 |
| Services register in platform setup and silently reject input | 3, 6 |
| Generic update listener can double reload | 6 |
| Duplicate-account checks occur after sign-in or SMS side effects | 6 |
| Phone config flow can create an entry without a usable refresh token | 1, 6 |
| Reconfigure flow is missing | 6 |
| Explicit `PARALLEL_UPDATES` is missing | 6 |
| Manifest redundantly requires Core's aiohttp | 6 |
| Runtime typing remains mostly `Any` | 6 |
| CI does not test minimum/current HA deliberately | 6 |
| Source coverage is 89%, below the >95% target | 6 |
| WebSocket integration test leaks an un-awaited startup coroutine | 4, 6 |
| Tracked coverage output and pytest-asyncio configuration dirty/warn in verification | 6 |

## Acceptance criteria

The hardening program is complete when all of the following are true:

1. A first-refresh outage retries setup; an established outage marks entities unavailable without erasing confirmed state.
2. A valid empty inventory succeeds and eventually removes confirmed stale devices.
3. For every field, a response that began earlier cannot overwrite a later accepted mutation.
4. Every logical user action is serialized per device, performs at most one confirmation refresh, and preserves later intent.
5. Phone and email accounts recover tokens correctly, persist rotation, and enter reauth only after recovery is exhausted.
6. Repeated WebSocket unauthorized cycles reach the threshold and trigger exactly one reauth flow.
7. Entry unload leaves no socket, producer, consumer, refresh, reconnect, or watchdog task running.
8. Diagnostics and debug logs contain none of the privacy sentinels.
9. Existing entity IDs, service calls, options, translations, and automations continue to work without migration.
10. Home Assistant 2024.8/Python 3.12 and current Home Assistant/Python 3.14 CI jobs satisfy every release gate.
