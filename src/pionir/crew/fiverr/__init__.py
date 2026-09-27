"""The Fiverr division: gigs drafted and orders prepared for the owner, who does every
Fiverr step himself.

Fiverr has no seller API, and automating the account breaks its terms. So NOTHING in this
package logs into, scrapes, posts to or messages on Fiverr. Orders arrive through Scrooge
(which reads Fiverr's notification emails) and everything goes out to the owner, on Discord:

- ``gigs.py``      ``fiverr.gigs``: one checked listing per service, as a card and a Markdown
                   file he pastes into Fiverr's gig editor; redrafted only when he asks.
- ``desk.py``      ``fiverr.desk``: each order from its events to a READY card with the files
                   and a drafted reply that HE uploads and sends; ``treasury.fiverr`` reads its
                   Fiverr-reported gross (never our revenue records).
- ``prices.py``    no AI sets a price: the site's own price grossed up for Fiverr's fee, or
                   the owner's reply; anything else is a proposal on his card.
- ``research.py``, ``data.py``, ``site.py``, ``health.py``: the four producers.
- ``checks.py``    what every deliverable passes before a card is posted.
"""
