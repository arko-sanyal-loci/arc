# Billo Tech and Evolution Vapes review note — 22 November 2026

This is a repository evidence and launch-status note. Billo Tech's static
research homepage and protected Arc continuity service are deployed. The
Evolution Vapes WordPress site has not been replaced. No company was
incorporated or contract signed.

## Billo Tech

- `https://billo.tech/` serves the static research homepage from the VPS. The
  published source is `playground/billo-tech/index.html` plus `site.css`. The
  page describes Billo Tech as a working research identity and labels
  prototypes and measurement limits.
- On 22 November 2026, the homepage and `/status` returned HTTP 200. The
  protected `/arko/chat` route returned HTTP 401 when checked without a login,
  as expected. The homepage links directly to chat and status.
- Before the landing changes, two dated VPS archives were saved under
  `/var/backups/`: `billo-tech-landing-before-20261122.tar.gz` and
  `billo-tech-landing-before-links-20261122.tar.gz`. They preserve the previous
  live landing files for rollback. Their SHA-256 values are respectively
  `731c30067548a629854f90ad130a41940d71bbee29058559ed44a73858793022` and
  `de5751d894d51d46b7855f886a534d51675e2f2d33a546ef3a05f9843b8ea949`.
- At a 22 November health check, the VPS accepted workstation
  telemetry sequence 972. The workstation was online, both inference lanes
  were ready, and the VPS journal verified 583 records. This proves the
  continuity telemetry path and journal integrity at that check; it does not
  prove live media capture or cross-runtime LOCI-to-Billo memory replication.
- A later 22 November read-only check returned finite 768-dimensional Nomic
  embeddings from both the local endpoint and secured VPS tunnel. Arc's
  semantic search returned one match for the health query. This verifies the
  query/embedding/retrieval path; it does not prove that every agent or system
  has an identical or complete index.
- On 22 November, `arc-interface.service`, `arc-monitor.service`, and
  `arc-nomic-tunnel.service` were enabled; the memory-sync, telemetry, evidence
  intake, and workstation-backup timers were enabled; and systemd user
  lingering was on. These user services can start at boot before an
  interactive login. Media capture itself remains user-started and opt-in.
- A later read-only V4L2 capability check found `/dev/video1` is the frame
  capture node and `/dev/video2` is metadata-only. The monitor and desktop
  overlay now exclude metadata-only nodes; a restarted monitor reports only
  `/dev/video1`. A one-frame local GStreamer test then decoded the connected
  camera at 1280×960 to a discard sink; no image file was created. The capture
  page now requests up to 1080p and forwards frames up to 1280 pixels wide,
  preserving aspect ratio. Three microphone inputs are detected, all
  `SUSPENDED`; no browser capture session or evidence intake is active. VPS
  telemetry sequence 1050 was accepted after the inventory correction. This
  verifies the direct UVC frame path and the monitor inventory, not browser
  permission, microphone capture, or end-to-end evidence intake with
  user-supplied media.
- The connected USB camera enumerates as a generic UVC device. No DJI Osmo
  device appeared in the workstation USB inventory at this check. DJI lists
  Wi-Fi 6 at 2.4/5 GHz and BLE 5.1 for Osmo 360 connectivity, and describes
  USB file transfer; DJI's current webcam compatibility list names selected
  Action/Pocket models but not Osmo 360. Thus wireless connectivity alone does
  not establish a live webcam feed, and Osmo 360 UVC compatibility remains
  unverified.
- Provider credentials were removed from the tracked `config/arc_config.yaml`
  and the persistent Arc service now accepts keys from the owner-only
  `~/.config/loci/arc-provider-keys.env`. That file is empty until the owner
  adds real keys. A read-only xAI model-list request rejected the previously
  configured value as invalid; a Nexos model-list request returned HTTP 403,
  which does not establish whether its key or the network edge caused the
  rejection. Neither external inference fallback is verified as usable. The
  health API now separates endpoint reachability from `api_ready`; the local
  provider is ready, while Nexos and xAI report no configured credentials and
  `api_ready: false`.
- The static Billo page does not establish that a Billo company, production
  API, worker, or integrated Billo memory platform exists. Those remain
  outstanding.

## Evolution Vapes — Bangladesh business

- The owner identifies their business as the Bangladesh Evolution Vapes at
  `https://evolutionvapes.com/` and says the Facebook page username `evovbd`
  belongs to the same business. `evolutionvapes.co.uk` is a separate UK
  franchise and is not the owner's business. Earlier drafts incorrectly
  connected the two; that reference is withdrawn.
- The live `.com` domain currently returns HTTP 200 and is an active Hostinger
  WordPress site, but its page is still the generic “Welcome to WordPress”
  starter page. The local file is a review-only placeholder. No WordPress
  content has been changed.
- A 22 November 2026 read-only check found one account database using 3 MiB of
  its 3 GiB allowance. Hostinger's account API reports no database assigned to
  `evolutionvapes.com`, and does not expose the database's tables or a SQL
  export. The public WordPress REST API returns one published `Hello world!`
  starter post and no published pages. This indicates starter WordPress content;
  it does not prove that the underlying database is empty. No database was
  deleted or changed.
- Hostinger's domain record shows `evolutionvapes.com` was registered on
  11 October 2018. Its Hostinger portfolio entry dates to 11 January 2022; that
  later account date does not change the 2018 registration date. Domain tenure
  supports the owner's recollection of a long-running business presence, but by
  itself does not prove when trading began.
- A direct share URL for the owner's Google Business Profile was not confirmed,
  and the supplied Facebook page could not be fetched by the web reader. No page
  posts, review text, or profile details were independently inspected.
  Search results surfaced third-party directory copies, which are not treated as
  authoritative. No address, hours, phone number, rating, or review text from
  those directories has been copied into a site draft.
- Hostinger's API did not expose an operation for downloading a full WordPress
  files-and-database backup or SQL export. A current recoverable full backup is
  still required before any homepage replacement; obtain the website archive
  and database dump through hPanel. Bangladesh-qualified counsel must also
  review the intended page and product category before publication because the
  current law's application to the proposed business page is unresolved.

## Shopify and Loyverse evaluation

- Loyverse lists Shopify integrations in its marketplace. The listed Shopify
  connector is supplied by SKUPlugs, a separate SaaS provider, and describes
  catalog, inventory, order, and customer synchronization. Loyverse's own setup
  guide says marketplace connections redirect to the provider and require the
  user to grant permissions. This is a marketplace-mediated third-party
  integration, not built-in synchronization in Shopify or Loyverse core.
- Shopify's Catalog policy excludes tobacco and vaping products. Bangladesh is
  not in Shopify's supported-country list for Shopify Payments, so the merchant
  would need a third-party payment provider. Shopify's ENDS guidance also makes
  the merchant responsible for complying with local law. These constraints do
  not establish that a third-party gateway will accept this product category or
  that local law permits the intended store.
- No Shopify store was created, no Loyverse app was connected, and no paid
  integration was activated. Do not migrate the WordPress site or remove its
  database based on this evaluation.

## Bangladesh publication constraint

The official gazette lists Act 91 of 2026, effective 10 April 2026. The
published amendment repeals the 2025 Ordinance, including its express broad
ENDS/vape sale-and-promotion prohibition. Section 5 continues to prohibit tobacco
product advertising in websites, web pages, internet media, and social media; it
defines advertising as commercial activity, directly or indirectly, for the
purpose of promoting a tobacco product or its use. The exact treatment of
vape-specific testimonial marketing under the amended text is not settled by the
sources reviewed here, so this note makes no legal conclusion about its
permissibility. Bangladesh-qualified counsel should determine how the current
law applies to the intended page and product category. Separate import, product,
licensing, payment, and age-related requirements also remain unverified.

Because the planned page concerns a vape business and the law's application to
that business is unresolved, the branded page is not ready to publish. Keep the
current WordPress site unchanged until counsel reviews the intended content and
the owner confirms the exact Google Business Profile. The local HTML is now a
review-only placeholder, not the former store-link concept.

## Billo Tech concept and collaboration draft

- The Billo Tech showcase is a self-contained HTML/CSS research site. The
  protected Arc continuity chat is a separate deployed service, not a Billo
  production API or a live private-memory replication service.
- The public-redacted collaboration draft is review material, not a signed
  agreement. Company registration, ownership, authority, and legal review remain
  outstanding.
- The AI legal-recognition paper is a research outline, not a peer-reviewed
  article or legal opinion.

## Sources

- [Bangladesh Government Press: official Gazette listing for Act 91 of 2026](https://www.dpp.gov.bd/bgpress/bangla/index.php/document/get_extraordinary/61244)
- [Act 91 of 2026, unofficial English translation of the Gazette](https://assets.tobaccocontrollaws.org/uploads/legislation/Bangladesh/Bangladesh-TC-Amdt.-Act-2026.pdf)
- [DJI Osmo 360 specifications](https://www.dji.com/360/specs)
- [DJI Osmo 360 FAQ](https://www.dji.com/360/faq)
- [DJI webcam compatibility guide](https://repair.dji.com/help/content?customId=en-us03400006962&documentType=artical&lang=en&paperDocType=paper&re=US&spaceId=34)
- [Hostinger: download a website backup](https://www.hostinger.com/support/5981435-how-to-download-backups-at-hostinger/)
- [Loyverse Marketplace: Shopify by SKUPlugs](https://loyverse.com/marketplace/shopify-skuplugs)
- [Loyverse: connect apps from the marketplace](https://help.loyverse.com/help/connect-apps-from-marketplace)
- [Shopify Catalog product requirements](https://help.shopify.com/en/manual/shopify-catalog/requirements)
- [Shopify Payments supported countries](https://help.shopify.com/en/manual/payments/shopify-payments/supported-countries)
- [Shopify guidance for ENDS products](https://help.shopify.com/en/manual/compliance/legal/ends)
