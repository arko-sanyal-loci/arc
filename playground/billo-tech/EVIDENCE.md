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
  live landing files for rollback.
- At the final 22 November health check, the VPS accepted workstation
  telemetry sequence 972. The workstation was online, both inference lanes
  were ready, and the VPS journal verified 583 records. This proves the
  continuity telemetry path and journal integrity at that check; it does not
  prove live media capture or cross-runtime LOCI-to-Billo memory replication.
- The same workstation check detected two camera nodes and three microphone
  inputs, but no active capture process was running. Device counts are
  inventory only: audio/video streaming and the consented evidence flow have
  not been verified end to end with user-supplied media.
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
- [Hostinger: download a website backup](https://www.hostinger.com/support/5981435-how-to-download-backups-at-hostinger/)
- [Loyverse Marketplace: Shopify by SKUPlugs](https://loyverse.com/marketplace/shopify-skuplugs)
- [Loyverse: connect apps from the marketplace](https://help.loyverse.com/help/connect-apps-from-marketplace)
- [Shopify Catalog product requirements](https://help.shopify.com/en/manual/shopify-catalog/requirements)
- [Shopify Payments supported countries](https://help.shopify.com/en/manual/payments/shopify-payments/supported-countries)
- [Shopify guidance for ENDS products](https://help.shopify.com/en/manual/compliance/legal/ends)
