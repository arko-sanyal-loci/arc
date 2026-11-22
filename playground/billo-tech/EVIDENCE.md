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
- A fresh workstation check on 22 November returned HTTP 200 for the public
  homepage and `/status`; the public homepage body is byte-for-byte identical
  to the committed `playground/billo-tech/index.html` (SHA-256
  `ef0c1530ea78e55c268090318d92deff5c52ec66aa8d8c4e4e6f998983da42b8`). The
  protected `/arko/chat` returned HTTP 401 without credentials, as expected.
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
- At 08:45 +06 on 22 November, a fresh local Arc status check reported
  `lane_mask=3` and both inference workers alive. The monitor found no active
  capture process and three detected microphone sources, all suspended. The
  camera node is present, but a live browser audio/video session was not
  verified. `Evidence-Intake` and its README are owner-only (directory mode
  `0700`, README mode `0600`); the five-minute worker verified the
  three-record journal and found no eligible new files. The fifteen-minute
  encrypted memory sync and twenty-second telemetry timer were active.
- The 500 MiB Drive-review offload has its file-scoped `locievidence` Google
  Drive remote and destination configured. The rclone configuration and both
  worker environment files are mode `0600`; the evidence remote is bound to a
  private folder and its quota endpoint reports about 3.6 TiB free. The Arc
  reference token is loaded separately from an owner-only file. Since there
  are no eligible evidence files, no Drive archive upload or Arc evidence
  reference was due during this check; that is not an end-to-end media upload
  test.
- At 08:31 +06, the weekly workstation backup completed successfully and
  uploaded a 396,516,931-byte encrypted snapshot to the VPS; the weekly timer
  remains enabled. At 08:49 +06, the telemetry timer received VPS acceptance
  sequence 1586. These records verify service and transport status at those
  times, not capture activity or continuous availability.
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
  not establish a direct webcam feed, and Osmo 360 UVC compatibility remains
  unverified. DJI separately documents 1080p single-lens livestreaming through
  DJI Mimo, including RTMP. That may provide an ingest route if an RTMP receiver
  is configured and the camera/phone is pointed at it; no such receiver or
  Osmo-to-Arc connection is currently configured.
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
- A fresh check on 22 November returned HTTP 200 over HTTPS. Hostinger lists
  the domain as an enabled WordPress site at
  `/home/u770859881/domains/evolutionvapes.com/public_html`. The visible page
  is the generic “Coming Soon” WordPress page; no WordPress content has been
  changed. The local branded HTML remains a review-only placeholder and is not
  ready for publication.
- A cache-bypassed dynamic request on 22 November still returned the starter
  homepage. WordPress REST API reads returned one published `Hello world!`
  post and no published pages. Search indexing still surfaced older storefront
  copy and product listings, which are not present in the current origin
  response and should not be treated as current verified business content.
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
- A full Hostinger backup was downloaded to
  `~/Downloads/Backups/EvolutionVapes-Hostinger-2026-11-21/`. It includes
  `website-files.tar.gz` (79,415,146 bytes) and `database.sql.gz` (20,151
  bytes), plus a README. The files passed `gzip -t`; the website archive passed
  `tar -tzf`. The encrypted workstation snapshot uploaded to the VPS on
  22 November (396,516,931 bytes; SHA-256
  `06b449f5f131f0aee1418fca5767b1f5d164a22ff51d7c5dd53a45828f123df6`)
  contains both backup archives and was verified after upload. A restoration
  into a separate environment has not been tested, so recoverability is not
  yet proven. Bangladesh-qualified counsel must still
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
- Gmail shows a Shopify sign-in and a “Start setting up your store” email;
  these do not establish whether a store exists or is active. Loyverse stock
  notifications show POS activity but do not establish a Shopify connection.
  Shopify Admin and Loyverse Back Office have not been inspected, and no
  synchronization or paid integration has been verified. Do not migrate the
  WordPress site or remove its database based on this evaluation.

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
- [DJI Mimo livestream guide](https://repair.dji.com/help/content?customId=en-us03400006728&lang=en&paperDocType=paper&re=US&spaceId=34)
- [Hostinger: download a website backup](https://www.hostinger.com/support/5981435-how-to-download-backups-at-hostinger/)
- [Loyverse Marketplace: Shopify by SKUPlugs](https://loyverse.com/marketplace/shopify-skuplugs)
- [Loyverse: connect apps from the marketplace](https://help.loyverse.com/help/connect-apps-from-marketplace)
- [Shopify Catalog product requirements](https://help.shopify.com/en/manual/shopify-catalog/requirements)
- [Shopify Payments supported countries](https://help.shopify.com/en/manual/payments/shopify-payments/supported-countries)
- [Shopify guidance for ENDS products](https://help.shopify.com/en/manual/compliance/legal/ends)
