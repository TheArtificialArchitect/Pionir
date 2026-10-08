"""The Marketplaces division: software sold where the buyers already look.

Three streams, each a store with its own buyer discovery:

- **Apify Store** - pay-per-event Actors. The core of each Actor is a small, offline,
  standard-library Python package built overnight by Daedalus like any Builds product; the
  Actor around it (``.actor/``, the Dockerfile, the wrapper that fetches and charges per
  result) is written HERE, deterministically, from the spec (apify_pack.py).
- **Chrome Web Store** - freemium extensions (ExtensionPay). Built only once the night builds
  can run JavaScript tests contained; until then a Chrome spec waits, and says why.
- **Shopify App Store** - no build and no publish yet: the pipeline prepares the app spec and
  a submission pack for the owner (shopify_pack.py).

The pipeline, per stream: ``scout`` (public marketplace data -> scored, de-duplicated
candidate specs) -> the Builds backlog (``specs.queue_for_build``) -> Daedalus and Claude's
review -> ``staging.stage_build`` (the Builds worker's one hook) -> ``packager`` (a listing
draft with honest claims, icons and screenshots, privacy answers) -> a PARKED publish
(``apify.publish`` / ``chrome.publish_update``, PRIVILEGED with ``requires_approval``: every
call waits for the owner's yes) -> ``watcher`` (runs, users and ratings, read only).

Every file lives under one folder (paths.py): ``~/.pionir/marketplaces`` by default,
``PIONIR_MARKETPLACES_DIR`` to move it. Nothing here publishes, spends or emails anything.
"""
