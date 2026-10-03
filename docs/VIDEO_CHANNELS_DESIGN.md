# Video channels: design (research only, nothing built)

Written 2026-10-02. Goal (Ian): a YouTube video maker and poster. Tutorials and history
facts, a couple of niches each run by its own bot, paid by ads and sponsorships, ending in a
page brands can sponsor. This file is research and design only. No account exists, no Google
API has been called, nothing posts. Every number below is either **measured on this machine
today** or **cited with a source and date**; anything I could not confirm first-hand is
marked UNCONFIRMED.

## 0. The honest summary

1. **The machine can already make the videos.** ffmpeg 8.1, Kokoro narration (the same
   `af_nicole` Moss speaks with) and a 1080p render are all measured and fast (section 1).
   Making video is not the hard part.
2. **The hard part is that a new channel earns nothing for a long time.** Ads need 1,000
   subscribers plus 4,000 public watch hours in 12 months (official page, fetched
   2026-10-02). Sponsors want an audience that does not exist yet. Plan for **months of
   zero**, and for ad revenue measured in tens of dollars, not hundreds, for a long while
   (section 2.5).
3. **Mass-produced AI video is exactly what YouTube demonetises.** "Inauthentic content"
   (mass-produced, generic, repetitive, template-made) is ineligible for ads. Surviving it
   is a quality and variation problem, so the design is **fewer, better, sourced, varied
   videos**, not a content flood.
4. **One hard external blocker, and it is Ian's to clear:** a new Google API project can
   only upload videos as **private** until Google audits it. Without the audit (or Ian
   publishing by hand in Studio) nothing posts automatically (section 2.3).
5. **Sponsorship money is real but late.** Typical small-channel rates are about
   $15-$50 per 1,000 average views per integration, so a channel averaging 2,000 views a
   video is worth roughly $30-$100 a sponsor slot. The sponsor page is cheap to build now,
   and it must say "channel too new" until real analytics exist.

## 1. What this machine already has (measured 2026-10-02)

| Piece | Finding |
|---|---|
| ffmpeg | 8.1.2 full build, `libx264`, `h264_nvenc`, `aac` all present |
| Render speed | 33 s of 1080p25 Ken-Burns slideshow with narration: **5.0 s** on libx264 `medium` crf 20, CPU only (about 0.15x real time; a 10-minute video is roughly 100 s) |
| Narration | `kokoro_onnx` + Moss's model files (`%LOCALAPPDATA%\Galatea\voice\kokoro-v1.0.onnx`, `voices-v1.0.bin`). `af_nicole`, CPU: 30.7 s of audio in 9.6 s (**real-time factor 0.31**; a 10-minute narration is about 3 minutes). Model load 1.2 s |
| Captions | `faster_whisper` is installed (Moss uses it). Not needed for our own narration: synthesise sentence by sentence and the exact timings are known, so burned-in captions come free |
| Images | `PIL`, `numpy`, `cv2`, `soundfile` present. **No torch, no diffusers, no ComfyUI**: there is no local image generation, and with Moss's gemma3:12b resident the card has about 1.8 GB free, so none is planned. `matplotlib` is not installed (PIL or a vector renderer for charts) |
| GPU | RTX 3060 12 GB; **10.4 GB in use right now** (Moss's resident model). i7-12700KF, 64 GB RAM |
| Models | `gemma3:12b` (resident, free to use for scripts), `qwen3-coder:30b`, `nemotron-3.5-lightning:30b-a3b` (CPU, 30 tok/s), `qwen2.5:7b-instruct`, `qwen3:4b-instruct`, `moondream` (image captioning, useful as a visual sanity check), `nomic-embed-text` |
| Screen capture | No `mss`/`pyautogui`, and capturing Ian's real screen is off the table anyway (private data, standing rule on his input). Tutorials will be **rendered**, not recorded (section 3) |
| Disk | about 740 GB free on C: |

Everything video-side is CPU, so it does not fight Moss for the GPU. The only GPU use is the
script model, and it is the one already resident.

### Reusable crew pieces

- `crew/blog.py`: `DailyPoster` (one draft a day, follow up on the approval, never assume
  published), the topic/seed rotation, goal-steered topic choice, retry-with-block-reasons.
  A channel worker is this shape with a video as the payload.
- `crew/instagram.py` + `adapters/instagram.py`: the closest precedent. A rendered artifact
  pinned by a sha (`card_sha`), a privileged `requires_approval` capability, the Discord
  card carrying the rendered image, an insights read for analytics. Video copies this.
- `crew/contentcheck.py`, `content_allowlist.json`: fail-closed text check (name allowlist,
  no personal data, no business numbers). Reused on titles, descriptions, scripts.
- `crew/grounding.py`: `claims_in` / `unbacked_claims` / `unknown_names`. The model of
  "a figure or name nothing recorded backs is blocked". The script checker reuses the idea.
- `crew/products.py`, `affiliate.py`: sponsor and product links, UTM discipline.
- `discord_gate.py`: approvals on the phone, only Ian's id counts, fail-closed, files as
  attachments (`multipart_body`). `social.card`: a PIL card renderer with bundled fonts.
- `economy/` (Bolts): cosmetic play currency, never money. A channel bot can earn Bolts for
  staged videos (new payout kinds), never anything that touches the gate.
- Crew catalogue (`crew/catalogue.json`): a division is a list entry with workers as
  entries; **a niche is a config line**. No new framework.

## 2. YouTube facts that decide viability

### 2.1 Getting paid by ads
Official page (support.google.com/youtube/answer/72851, fetched 2026-10-02): 1,000
subscribers **and** 4,000 qualified public watch hours in the last 12 months, **or** 1,000
subscribers and 10 million qualified public Shorts views in 90 days. Shorts and unlisted
views do not count toward the long-form hours. Creators must also accept updated terms in
Studio by 2027-01-31.

Secondary sources (vidIQ, Tella, others, 2026) also report a 500-subscriber "early access"
tier that unlocks fan funding only (no ads), and **a tightening from 2027-02-01 to 8,000
hours / 20M Shorts views**. I could not confirm the 8,000 figure on the official page: treat
it as UNCONFIRMED and re-check before planning around it. If real, getting in before it
matters.

### 2.2 The inauthentic-content policy
Official (support.google.com/youtube/answer/1311392, fetched 2026-10-02): content must
"Not be mass-produced, generic, repetitive, or manipulative." Named violations include
"Similar or repetitive content with low educational value, commentary, narratives, or
minimal variation" and "AI-generated content made with generic or unoriginal templates
giving the impression of mass production." Explicitly fine: the same intro and outro when the
bulk differs, and series where "each video has a distinct storyline, focus, or concept".
It took effect (renamed from "repetitious content") 2025-07-15.

AI narration alone does not disqualify a video. A channel that is **all** text-to-speech over
stock images, batch-uploaded, with no commentary or original value, is the high-risk shape.
That is what the cheapest pipeline would produce, so the design deliberately adds what the
policy rewards: original structure per video, a sourced argument or point of view, mixed
visual types, and a hard cap on cadence.

### 2.3 Uploading by API
Official (developers.google.com/youtube/v3/docs/videos/insert, fetched 2026-10-02): "All
videos uploaded via the `videos.insert` endpoint from unverified API projects created after
28 July 2020 will be restricted to private viewing mode." Lifting it needs a compliance audit.
`videos.insert` is now its own quota bucket, 100 calls a day by default (changed 2025-12-04
per secondary sources), which is far more than we need.

Consequence: **an unaudited project can stage a private upload, and Ian flips it to public
in Studio with one click.** That fits the approval rule exactly (he is the gate anyway) and
needs no audit. The audit can wait until volume justifies it, and it is also a place we could
be refused. Ian must create the channel and the Google Cloud project himself; I will not.

### 2.4 AI disclosure labels
Policy: creators tick "altered or synthetic content" when realistic content could be mistaken
for real footage or a real person (cloning someone else's voice, altered real events,
realistic scenes that did not happen). Not required for AI narration over illustrations,
AI scripts, animated or obviously unreal visuals (secondary sources 2026, consistent with
YouTube's own help text). Failure to disclose when required can cost Partner Program
standing. Our pipeline should avoid anything that needs the label (no fake historical
"footage", no synthetic photos of real people), and **say plainly in the description that the
narration is a synthetic voice**. Cheap, honest, and not required, so it is Ian's call (Q5).

### 2.5 What it actually pays
- **Ad RPM (what the creator gets per 1,000 views), secondary data 2026:** education
  median about $10 (spread $2-$20), history/documentary about $4-$10 with a US audience,
  tutorials about $4-$15, higher for professional skills. These are US-heavy medians; a
  global audience pays less.
- **Arithmetic to the threshold:** 4,000 watch hours at a 5-minute average view is about
  48,000 views. At $6 RPM that is **about $290 earned over the whole climb**. At $6 RPM,
  $4,000 a month needs about **670,000 views a month**. Ads alone will not be the $4k/month
  answer on a realistic horizon.
- **Time to first dollar:** unknown and wide. Faceless AI-style channels commonly take many
  months to reach 1,000 subs and 4,000 hours, and many never do; I have no data on this
  machine's output, so I will not invent a median. Honest planning figure: **6-18 months,
  with a real chance of never**, and the policy risk above on top.
- **Sponsorships (secondary, 2026):** 1,000-10,000 subscribers about $50-$500 per 60-second
  integration; about $500-$2,500 at 10,000 subscribers; typical $15-$50 per 1,000 average
  views, more in finance and B2B tech. Formula: (average views / 1,000) x niche CPM. Brands
  care about average views and engagement more than raw subscribers. Below a few thousand
  average views there is rarely a paying inbound.
- **What does pay earlier than ads:** affiliate links and our own products in the
  description (the existing blog/devto funnel into the paid APIs), since a tutorial viewer
  is a developer-shaped audience for exactly that. This is the better first dollar and the
  reason tutorials may out-earn history early.

### 2.6 Rights for history imagery and music
- **Images:** Library of Congress (check each item's Rights Advisement; "no known
  restrictions" is not a licence, record it), Wikimedia Commons (public domain and
  CC-BY/CC-BY-SA: BY needs credit on screen and in the description, SA is share-alike and
  is best avoided), Internet Archive (item by item). The pipeline must store the licence
  and source URL per asset and refuse any asset without one (fail closed, same as the
  content check). Photos of living people and trademarks are out.
- **Music:** YouTube Audio Library tracks are cleared for monetised YouTube videos only
  (not for reuse elsewhere), and some need attribution. Simplest and safest: **no music,
  or music we generate ourselves**; silence plus a narration bed monetises fine.
- **Facts are not copyrightable, wording is.** Scripts are paraphrased from sources and
  cite them; no passage is copied.

## 3. Pipeline design

Division `video`, one worker per niche, each a catalogue entry. The workers are code, not
personalities; they call the shared model only for words.

```
topic finder -> source gathering -> script -> script check (fail closed)
 -> narration -> visuals -> assemble -> thumbnail + title + description
 -> final check -> QUEUE FOR IAN (Discord card + preview) -> he approves
 -> private upload by API -> Ian sets it public (or audit later)
 -> analytics back to the leader -> topic finder learns what held viewers
```

1. **Topic finder.** Seeds per niche from data (a `topics.json` per niche), steered by the
   division goal exactly as `blog.py` does, and by measured retention once there is any. A
   topic is never reused. No hardcoded lists in code (the estate rule).
2. **Source gathering.** Fetch the actual source passages first (Wikipedia and its cited
   primary sources, Library of Congress, Internet Archive; for tutorials the library's own
   docs and the real code). The model writes **from the passages it is handed**, with a
   passage id on every claim.
3. **Script, grounded and fail-closed.** Every date, number and proper name in the script
   must appear in a passage the worker fetched; one that does not blocks the script and the
   reasons go back into the next draft (the `blog.py` retry). This is the `grounding.py`
   idea applied to a script, and it is the main defence against a 12B model inventing a
   date. The check runs on the exact final text. Harder calls may escalate to `claude -p`
   on Max (existing 10/day cap), never the API key.
4. **Narration.** Kokoro `af_nicole` (or a second voice per niche for variety), sentence by
   sentence, which also gives exact caption timings. Synthetic-voice line in the description.
5. **Visuals, no image model.** Per scene, one of: a licensed public-domain image with a
   slow pan (Ken Burns), a rendered map or timeline, a rendered quote or date card (the
   `social.card` machinery), or for tutorials **a rendered terminal and editor** (frames
   drawn with PIL from code the worker **actually ran** in the build sandbox, so the output
   on screen is the real output and not a model's guess). Mixed scene types per video is
   deliberate: it is variation the policy asks for. Every asset has a recorded licence.
6. **Assemble.** ffmpeg, 1080p25, burned captions, loudness-normalised. About 100 s per
   10 minutes of video. Also cut one or two 30-60 s vertical Shorts from the best scenes as
   a funnel to the long video (they do not count toward watch hours but do bring subscribers).
7. **Thumbnail, title, description.** Thumbnail rendered from a template plus the topic's
   best licensed image; the title and description pass `contentcheck`. Sources and licences
   are listed in the description (also good practice and good for trust).
8. **Approval, always.** A new privileged, `requires_approval` capability
   `video.youtube_upload`, `routable=False`, like `social.instagram_post`. The Discord card
   shows title, description, the script's source list, the first-failed-check log (empty),
   the thumbnail and a **small preview file** (480p, kept under a few MB; the gate's
   attachment limit must be checked before building, I did not find it stated in
   `discord_gate.py`). Approve here means "upload it private". The final public flip is
   Ian's click in Studio. Nothing is ever recorded as published without a returned URL.
9. **Analytics.** `youtube.analytics` read-only capability (views, watch time, average view
   duration, subscribers) feeding the division leader. Honest numbers only, "unknown" and
   not zero when it cannot read, same rule as the Instagram insights.
10. **Cadence and volume.** CPU render plus the resident model means no GPU contention, so
    the limit is quality and review attention, not hardware. Plan **1 long video a week per
    channel**, two channels at first, plus 2 Shorts a week. A flood teaches Ian to stop
    reading and trips the policy. Raise it only once retention data says it is working.

### Shorts or long-form
**Long-form (8-12 minutes) is the base.** It is the only thing that builds the 4,000 watch
hours, it allows mid-roll ads (from 8 minutes), and it is where sponsorship value sits. The
10-million-views Shorts route is not a plan for a new channel. Shorts are a cheap discovery
layer cut from the long videos, not a strategy.

### Niche config (data, not code)
```json
{"id": "history-fire", "title": "...", "kind": "history",
 "voice": "af_nicole", "length_minutes": [8, 12], "cadence_days": 7,
 "sources": ["wikipedia", "loc", "internet_archive"],
 "scene_mix": {"image": 0.5, "map": 0.2, "card": 0.2, "timeline": 0.1}}
```
A tutorial niche swaps `sources` for the docs it teaches from and adds the sandbox-run
screencast scene type. A third niche is one more entry.

## 4. The sponsor page

- **Where:** a static page on the existing Cloudflare Pages setup (a `/sponsor` page on
  dokazindustries.com, or its own project). Static means nothing to run. Deploy follows the
  existing manual-wrangler rule and Ian's go-ahead.
- **Media kit with real numbers only.** The page's figures are generated from the analytics
  capability's last real read, with the date of the read next to every number. Before the
  channel has data the page says **"This channel is new. Real audience numbers will appear
  here once there are any"** and shows no stats. It never shows an invented audience, a
  projected one, a fake brand logo or a testimonial. (Fail closed, the same lesson as the
  Techne labels and the anonymiser.)
- **Packages:** priced and described only once there is a measured average view count to
  price against. Until then the page offers a "talk to us" inquiry and nothing with a price.
  Likely shapes later: a 30-60 s integration, a pinned link in the description, a series
  sponsorship. Price from the formula in 2.5, shown as a range, never a promised result.
- **Inquiry form:** posts to a Cloudflare Worker that stores it and notifies **Ian**
  (email plus the Approvals tab). No auto-reply that promises anything, no auto-accept.
  Honeypot and Turnstile against spam. A sponsor deal is "money and public content": a
  human gate, as with everything customer-facing.
- **Disclosure:** sponsored segments get the YouTube paid-promotion checkbox and a spoken
  and written "sponsored by" line. A rule in the checker, not a habit.
- Ian dislikes cold calls and repeat cold emails: **the page is inbound only**. No outreach
  to brands from this system. If he wants outbound later it is a separate decision.

## 5. Build order (small, each slice proves something)

1. **One video, offline, by hand-run command.** `python -m pionir video make <topic>`
   produces a full 1080p mp4, thumbnail, description and source list into a folder, using
   only what is measured above. Proves: quality is watchable; script grounding works. Ian
   watches it. Nothing is uploaded. *This slice decides whether the rest is worth doing.*
2. **The script checker with real failure cases** (invented date, unsourced name, copied
   passage, missing licence), tests that fail with the guard reverted. Proves the model
   cannot slip a fact through.
3. **Tutorial scene type:** run real code in the sandbox, render the terminal frames.
   Proves the second niche is feasible without screen capture.
4. **Approval card with preview** and a stub `video.youtube_upload` that stages nothing
   public. Proves the human gate end to end (the Instagram path is the template).
5. **Niche workers in the crew catalogue** (two entries) with topic rotation and the
   goal-steering. Proves the cadence and the per-niche config line.
6. **YouTube upload (private) and analytics read** once Ian has a channel and a Google
   Cloud project and has put the credential files in `~/.pionir/secrets` (never in chat).
   Proves the real path, which is the one that has never run (the estate's usual failure).
7. **Sponsor page** with the "channel too new" fail-closed state, then the live numbers
   once slice 6 returns real analytics. The page can ship before any audience exists, as an
   honest placeholder and an inbound address.

Stop and re-assess after slice 1: if the first videos are not watchable or not
policy-safe, no amount of plumbing fixes that.

## 6. Questions for Ian, ranked by what blocks the most

1. **Will you create the channel(s) and the Google Cloud project, and publish each video
   yourself in Studio (the approve-then-click flow)?** Blocks slice 6. I cannot and will not
   create the accounts. This also decides whether we ever need the API audit.
2. **Which two niches, and what flavour?** History facts: which era or angle (a recurring
   series with a distinct point of view survives the policy better than "random facts").
   Tutorials: which subject (the Dokaz API products, general Python/PowerShell, tools you
   know). Tutorials feed the paid-API funnel; history is the broader audience.
3. **Faceless fully synthetic, or will you ever record a short intro or a voiceover
   yourself?** A human voice or a human point of view is the strongest defence against the
   inauthentic-content policy. Faceless and synthetic works but is the riskiest shape.
4. **Channel names and branding.** Under Dokaz Industries, or separate brands? Affects the
   sponsor page and the Google account.
5. **AI-disclosure stance.** Say "narrated by a synthetic voice" in every description
   (recommended), or only tick the label when YouTube's rule requires it?
6. **Sponsorship appetite.** Any categories you will not take (gambling, crypto, adult,
   political)? Used as a deny filter on inquiries.
7. **How long are you willing to run this at zero?** The honest horizon is months. Is a
   two-channel, one-video-a-week, low-attention version acceptable for 6 months before we
   judge it, or should it be dropped faster if the first dozen videos get no views?
8. **Moss's role.** She steers topic and cadence through the division goal, as she does the
   blog, and never publishes. Is that right, or do you want her reviewing scripts too?

## Sources (fetched or searched 2026-10-02)

- YouTube Partner Program eligibility: https://support.google.com/youtube/answer/72851
- YouTube monetization policies, inauthentic content: https://support.google.com/youtube/answer/1311392
- YouTube Data API `videos.insert` (private restriction, quota): https://developers.google.com/youtube/v3/docs/videos/insert
- Secondary (UNCONFIRMED where noted): vidIQ and Tella YPP guides 2026 (early-access tier,
  2027 change); fluxnote / air.io RPM-by-niche studies 2026; 1of10, tryspansa, ytmoneycalculator
  sponsorship-rate guides 2026; narrationbox / shortsfast AI-disclosure guides 2026;
  Wikimedia Commons and Library of Congress reuse guidance.
