# Hostinger migration user guide

This guide is the human-safe procedure for moving Evolution Vapes work forward
without allowing Arc, an agent, or an accidental command to damage production.

## One-screen safety rule

**If the command contains `production`, `evolutionvapes.com`, DNS, delete,
drop, overwrite, reflash, or migrate, stop and obtain explicit owner approval.**

Use `test.evolutionvapes.com` for experiments. Never use the production domain
as a test target.

## What exists today

| Item | Status |
|---|---|
| `evolutionvapes.com` | Existing Hostinger WordPress site; unchanged |
| `test.evolutionvapes.com` | Isolated staging location |
| Hostinger backup | Downloaded, archive-checked, and isolated-restore tested |
| Shopify | Not created or connected |
| Loyverse/SKU sync | Not connected or implemented |
| Production DNS | Not changed |
| Production database | Not deleted or migrated |

## Operator workflow

### 1. Create a migration ticket

Record:

- date/time in UTC;
- operator and approving owner;
- exact target (`staging` or `production`);
- intended change;
- backup identifier and SHA-256;
- rollback action;
- expected downtime, if any.

For a planned outage, use the private Dispatch issue board. The issue must
show as saved before maintenance begins.

### 2. Take or verify a backup

Before any write:

- download the Hostinger website archive and database export;
- verify the archive with `gzip -t` and `tar -tzf`;
- calculate SHA-256 hashes;
- store the encrypted copy in the approved private backup location;
- record the backup manifest locally.

Do not treat a successful download as a successful restore. A disposable,
network-isolated restore must pass before destructive work is considered.

### 3. Work only in staging

Use the staging root and staging hostname. Check:

- homepage and navigation;
- product/catalog data;
- forms and email behavior;
- mobile layout;
- checkout/payment test mode only;
- age/legal notices;
- robots/noindex settings;
- logs for unexpected writes or external calls.

Do not connect live payment credentials, live webhooks, or production
customer data to staging.

### 4. Review content and compliance

Before publication, obtain separate approval for:

- exact business identity and contact details;
- product claims and images;
- payment provider eligibility;
- age verification;
- Bangladesh tobacco/vape advertising and sales requirements;
- privacy, returns, delivery, and customer-data handling.

The existing evidence does not establish that the intended vape storefront is
legally ready to publish.

### 5. Prepare a cutover packet

The packet must contain:

```text
backup/
  website.sha256
  database.sha256
  restore-check.txt
staging/
  staging-url.txt
  smoke-test.txt
commerce/
  catalog-map.csv
  sku-conflicts.csv
  sync-dry-run.txt
cutover/
  dns-before.txt
  dns-after-plan.txt
  rollback.txt
approvals/
  owner-approval.txt
  compliance-review.txt
```

Never put tokens, passwords, private keys, raw customer data, or raw personal
media in this packet.

### 6. Cut over only after approval

The human owner confirms all of the following:

- backup hash and restore check passed;
- staging smoke test passed;
- content/compliance review passed;
- commerce/payment test passed in the correct mode;
- DNS TTL and rollback plan are understood;
- maintenance window is active;
- production write is explicitly authorized.

Arc must report the exact target and intended write before executing it.

### 7. Verify and monitor

After cutover:

- check DNS, HTTPS, homepage, forms, and checkout;
- check error logs and webhook delivery;
- verify one real operational workflow without exposing customer data;
- compare the deployed manifest hash;
- record the result in Dispatch;
- keep the old backup and rollback path intact until the owner closes the
  migration.

## Arc operating contract

Arc should use this response pattern before a risky action:

```text
TARGET: staging | production
ACTION: exact write/change
BACKUP: identifier + hash
IMPACT: expected effect
ROLLBACK: exact reversal
APPROVAL: required | received
```

If any field is unknown, Arc must stop and ask instead of guessing.

Arc must not:

- silently switch a staging hostname to production;
- delete a WordPress database or site files;
- change DNS because a build succeeded;
- publish unreviewed vape/tobacco content;
- put credentials into source files or prompts;
- claim Shopify/Loyverse synchronization is live without a verified dry run;
- upload raw media or private customer data through LociSync.

## LociSync boundary

LociSync is appropriate for encrypted migration manifests, provenance,
approval records, hashes, and scoped continuity notes. It is not the primary
transport for a WordPress archive, SQL dump, or raw motorbike videos.

For private media, use this order:

1. copy to an owner-only local staging directory;
2. verify file counts, sizes, and hashes;
3. encrypt the archive;
4. upload only to an explicitly approved private vault;
5. store the hash and retention decision in the migration manifest;
6. never expose the archive through the public website or broad agent memory.

## Stop conditions

Stop immediately and ask the owner if:

- the target is ambiguous;
- the backup cannot be verified;
- the device or domain identity differs from the manifest;
- a command would erase, reflash, overwrite, or delete;
- a secret is requested in chat or source;
- production and staging data are mixed;
- a legal or payment requirement is unresolved;
- a human reports lost, personal, or irreplaceable media.

## Completion definition

The migration is complete only when the owner has a saved backup, a verified
restore path, a passing staging review, an approved cutover record, successful
post-cutover checks, and a documented rollback result. “Build succeeded” is
not a migration completion signal.
