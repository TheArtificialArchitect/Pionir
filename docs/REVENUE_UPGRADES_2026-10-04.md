# Money upgrades — 2026-10-04

Everything below is built, tested and merged to `main` in Pionir and Scrooge.
**Pionir is pushed. Scrooge is merged locally but NOT deployed** — the safety
classifier blocks me from reading the production database, so the migrations and the
deploy are yours (section B). Run the steps in order.

---

## Your steps

### A. Night builds + API builder (elevated PowerShell, once)

The sandbox's Python packages were installed with an admin-only permission list, so
pytest and fastapi looked like empty folders: every night build died at startup. Re-run
the (now fixed) setup script as administrator — it resets the permissions and proves
the imports as `pionir-builds`:

```powershell
cd C:\src\Pionir; powershell -ExecutionPolicy Bypass -File .\tools\setup-build-sandbox.ps1
```

If you only want the quick repair: `icacls "C:\ProgramData\PionirBuilds\python\Lib\site-packages" /reset /T /C /Q`
(should end "Failed processing 0 files"). If the script prints a yellow
"node … LEFT OUT of the record" line, that line names why the API builder still can't run.

### B. Scrooge go-live (normal PowerShell, one block at a time)

The three migrations only ADD tables/columns (checked). Each file's header lists
before/after checks. Run each migration once — the column one fails if repeated.

```powershell
cd C:\src\Scrooge\worker; npx vitest run; if ($LASTEXITCODE -ne 0) { Write-Host "TESTS RED - stop" -ForegroundColor Red }
```
(Under heavy load a few Python/OpenCV tests can time out; rerun just those files and they pass.)

```powershell
cd C:\src\Scrooge\worker; npx wrangler d1 execute scrooge --remote --file=migrations/2026-10-04-newsletter.sql
cd C:\src\Scrooge\worker; npx wrangler d1 execute scrooge --remote --file=migrations/2026-10-04-checkout-recovery.sql
cd C:\src\Scrooge\worker; npx wrangler d1 execute scrooge --remote --file=migrations/2026-10-04-testimonials-referrals.sql
```

```powershell
cd C:\src\Scrooge\worker; npx wrangler deploy; if ($LASTEXITCODE -eq 0) { cd C:\src\Scrooge; git push origin main }
```

### C. Two secrets

```powershell
cd C:\src\Scrooge\worker; $b = New-Object byte[] 16; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); $k = ($b | ForEach-Object { $_.ToString('x2') }) -join ''; $k | npx wrangler secret put INDEXNOW_KEY; "check: https://api.dokaz.net/$k.txt"
```

```powershell
cd C:\src\Scrooge\worker; npx wrangler secret put POSTAL_ADDRESS
```
`POSTAL_ADDRESS` is the mailing address printed at the foot of every newsletter (US
anti-spam law requires one; a PO box or virtual mailbox is fine). **Your decision — I
did not invent one.** Until it is set the newsletter queues nothing.

### D. Stripe dashboard (abandoned-checkout recovery)

Developers → Webhooks → endpoint `https://api.dokaz.net/hooks/stripe` → Update details /
Edit destination → Select events → tick **`checkout.session.expired`** → Update endpoint.

### E. Optional: Resend bounces

Set `RESEND_WEBHOOK_SECRET` and point a Resend webhook at `https://api.dokaz.net/hooks/resend`
so complaints and hard bounces leave the newsletter list automatically.

### F. Restart, after B

In **Pionir Desktop**, press **Restart** on **Pionir** (the server — new capabilities and
card kinds) and on **Crew** (new workers + all fixes). Do B first so the new workers find
their Scrooge endpoints.

---

## Decisions waiting on you

1. **116 outreach drafts** sit in Scrooge's own approval queue with outreach "armed",
   which contradicts "no cold leads". Approve a batch, or disarm outreach.
2. **Fiverr:** you priced 4 gigs, but nothing records whether they're published, and the
   desk has seen 0 orders. Confirm they're live on Fiverr.
3. **Recovery-email consent:** Stripe suggests asking consent at checkout for abandonment
   emails. Default: leave it off (one email per order, each approved by you on Discord).
4. **Instagram shows 0 reach on all 8 posts** while the bio link brought 23 visits. The
   token is the right account (`dokaz_industries`). Open one post's Insights in the app: if
   the app shows reach but the API says 0, the Meta app needs advanced access for
   `instagram_business_manage_insights`.
5. **Blog drafts:** gemma3:12b still invents invoice numbers/companies; the check rightly
   blocks them. Repairs + placeholders should lift the pass rate. If it stays low, the
   option remains one Claude (Max) drafting call a day.

---

## What was wrong (5 fixes)

| # | Blocker | Fix |
|---|---|---|
| 1 | **The Gumroad shelf never saw a product.** Gumroad sends cents as `0.0`; the check wanted an int, so all 5 products were "malformed" on every run since 09-26 (414 runs) — a sale would never have been announced, and the worker showed green. | Whole floats read as ints; malformed rows now raise a loud alert. |
| 2 | **Night builds could not start.** Sandbox packages unreadable → Daedalus crashed on import, pytest preflight falsely passed; VRAM gate 31–181 MB too strict. | Setup resets permissions and proves imports as the sandbox user; a namespace package counts as missing; gate 9,000 MB; repeated not-configured warnings logged hourly, not every 5 min. |
| 3 | **No blog post ever linked to a product**, and 15/17 drafts were blocked. | Every post ends with a tagged link to the relevant paid offer (Obol for invoice topics, /hire otherwise); model links are unlinked and example.com replaced before the check (the check itself is unchanged); prompt asks for `{placeholders}` instead of invented data. |
| 4 | **Unreadable Fiverr emails were acknowledged and dropped** (7, one today) — a buyer message could vanish. | They become an owner card; acknowledged only after the card posts. |
| 5 | **Sales and upgrades were untraceable, and the blog was cut off.** 429 upgrade link and template links had no UTM; no home-page link to the blog. | Tagged everywhere; blog in the home nav and docs footer; site-wide product CTA under every post; each API guide links its free tool. |

Also fixed on the way: leader reports refused for true numbers ("1 client order",
"one declined order"); "1 refunded order" escaped the claim check entirely; a Scrooge test
that only failed in October.

## What's new (5 income paths)

1. **Abandoned-checkout recovery** — Stripe keeps a recovery link when a /hire checkout
   expires; the crew proposes ONE fixed-template email per order, approved by you on Discord.
   Digest counts expired, sent, recovered and recovered revenue.
2. **SEO engine** — RSS feed, real `lastmod` in the sitemap, and an IndexNow ping on every
   publish so Bing and others index new posts within hours.
3. **Opt-in newsletter** — double opt-in form on the home page, blog and every post;
   one-click unsubscribe; a weekly digest assembled from the week's posts and products
   (no model), checked, and sent only after your approval. Max 50/day within Resend's free tier.
4. **Testimonials + referrals** — 3+ days after delivery, one approved feedback email with a
   private link; a testimonial shows on /hire only with the client's consent AND your
   approval; each client gets a referral code worth $25 off their next order (recorded only;
   you apply it by hand).
5. **Demand → backlog loop** — `products.demand` ranks APIs, tools, blog topics and backlog
   items daily from measured usage and traffic (unknown stays UNKNOWN, never 0). It steers
   what Daedalus builds and what the blog writes about when Moss hasn't named something, and
   proposes new products to you as cards (max 1/day).

## Known gaps (not bugs, noted for later)

- Scrooge drops `utm_content`, so per-tool click-throughs and 429s per endpoint are UNKNOWN
  to the demand loop. Storing them is a small Scrooge change.
- Moss's division goals only steer three places (blog topic, build pick, leader brief).
- Atani's life loop has been failed since 09-11 (KeyError 'emotional-trust'); not revenue.
- An open (not yet lost) Stripe dispute isn't tracked; deny a feedback card if you know of one.
