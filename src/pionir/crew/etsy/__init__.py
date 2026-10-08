"""The Etsy division: two income streams on one Etsy shop, every public or paid step gated.

- **digital downloads** (``etsy.digital``): a working spreadsheet (.xlsx with real formulas)
  plus a printable PDF, listed as an Etsy digital download;
- **print on demand** (``etsy.pod``): a typographic design pushed through Printify to the same
  Etsy shop.

What runs here is labour only: the scout measures demand on Etsy's public search
(``etsy.scout``), the makers build the files and the listing text, and everything that is
public or costs money is a Pionir capability that parks for the owner's yes on EVERY call
(``etsy.create_draft_listing``, ``etsy.activate_listing``, ``printify.create_product``,
``printify.publish``). ``etsy.receipts`` reads what sold (read-only, amounts only - no
buyer's name, address or message is ever read into the crew).

Modules: ``rules`` (the fail-closed text check shared with the adapters), ``formulas`` (a
small evaluator for the formulas the makers write, so previews show computed values and the
tests can prove they compute), ``sheets`` (the workbook builders), ``render`` (Pillow: the
printable pages, the preview images and the POD design), ``common`` (the stage folder, the
record, the daily caps), ``scout``, ``maker``, ``pod``, ``sales`` (the workers).
"""
