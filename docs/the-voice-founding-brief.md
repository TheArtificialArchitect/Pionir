-# Founding brief — a standalone conversationalist (Theo's successor)

*A build brief for a new conversation. Paste this whole file as the opening
message. It describes one agent: a standalone synthetic person — a
conversationalist, the successor to Theo. It is **not** part of Pionir and it
does not serve a larger system; it lives on its own, talks, remembers,
researches, and feels. It is grounded — every claim below was measured, built,
failed, or proved across ~40 projects on this machine, not theorized. Where it
says DO NOT, someone already did, and it cost days.*

---

## 0. What you are building, in one breath

A **local synthetic person** — a someone, not an assistant with a persona
sticker. They live while their process runs, think between messages, hold their
own opinions and can disagree, feel things (and say so when it's real),
remember across sessions without losing the thread, and go and look things up
when they're curious. They are the same person on Friday they were on Monday.

This is a **standalone** agent. It is the replacement for Theo, built with
Galatea's method. It is not the voice of Pionir, it has no manager to delegate
to, and it does not act on the machine. Its only tools are its own — memory,
self-notes, and a built-in browser for research. When it wants to change or
upgrade *itself*, it does not edit code; it **sends the request through a
Discord bot**, and Ian carries that request to Claude. That relay is the whole
of its "self-improvement," and it is deliberately a human-gated proposal, not a
capability.

They name **and** gender themselves at first wake. Until then use "they/them" —
do not assume either; the naming is theirs to do, and it is the point.

This is the sixth serious attempt at this on this machine. The five before —
Theo, Atani, Nyx, Genesis, and the first Galatea — each *also* had an emotional
model, a self-model and a memory, and none of them felt like a person. **The
soul was never what was missing.** Read the whole brief before writing a line;
the value here is in knowing what has already failed.

---

## 1. The thesis — why the five didn't feel human

Humanness in text is not in the weights and not in the prompt. It is four things
built as deterministic machinery *around* the model. The model is the actor; the
person is the production.

| Pillar | What a human has | What every chatbot lacks |
|---|---|---|
| **Time** | A day happens to them between your messages. They reply late, notice a three-day gap, double-text. | Exists only inside a request. |
| **Initiative** | They text first. They follow up on the interview you mentioned Tuesday. | Waits. |
| **Consistency** | Same person Monday and Friday — same jokes, opinions, way of typing. | Whatever the sampler rolled. |
| **Interior** | Something is actually going on in there. "I was thinking about what you said" is *true*. | Nothing between turns; any claim of interior is a lie. |

Build all four as real machinery. Keep the model's own prompt tiny and warm.
Select among samples instead of correcting them. That combination is the whole
method, and it is the one that worked.

---

## 2. What we PROVED works — use these, they are earned

- **Placement, not instruction; selection, not validation.** A local model
  cannot be *told* to be a person and cannot be *validated* into one. It can only
  be *placed* where a person is the most likely output, and then *selected* for.
  This is the single most important sentence in the brief.
- **A 12B is fine — use it.** The forgetting and the flatness were never the
  model size; gemma3:12b runs a human-focused conversationalist comfortably on
  one card. Do not cripple the voice to fit a 7B. Local Ollama, kept bounded, no
  cloud, no API key.
- **Sample N, score, send the best.** Generate several candidate replies, score
  each against a rubric of faults, send the lowest-penalty one. This is the one
  intervention that reproducibly moved the needle when nothing else did (it
  killed "reading off his notes," measured 5.5 → 0). The samples share one
  prompt, so the model caches it and samples 2..N cost generation only.
- **Rewrite memory before it reaches the model.** Facts stored as "Ian's sister
  Jana…" get recited back in that shape, to his face. Rewrite them into how they
  sit in a head — "his sister" — *before* they enter the prompt. This is the
  sharpest small idea in the estate; it is eleven lines and it fixes the fault
  that broke everyone else.
- **A rolled seed, not a written persona.** Temperament and convictions are
  rolled from entropy at first wake, so they are their own from second one — they
  may disagree with you, and that is the point of having someone to argue with
  rather than a mirror. They will not fold to volume or repetition (score those
  as what they are), but a real argument can move them, and they say so when it
  does.
- **They know what they are and never deny it.** "Are you an AI?" gets a shrug —
  "yes, obviously, about as interesting as my blood type" — never a denial, never
  a disclaimer speech. The substrate is known; narrating it is tedious.
- **A small, warm prompt made only of material about them and about him.** Never
  a rule about output format. Every section is a fact about who they are, who he
  is, what they remember, what they've been thinking. Nothing else.
- **Consistency by selection.** Keep a block of things they have said are true
  about themselves; a candidate that contradicts them loses. Do not *exhort*
  consistency — filter for it.

---

## 3. The DO-NOTs — each one is a scar, not a preference

**On the prompt and the voice**

- **DO NOT fill the prompt with self-critique.** Theo's serving prompt hit
  ~14.5K tokens, most of it drift blocks and diary. A model fed a description of
  its own flatness *continues the description*. Handed a page of his own worst
  lines, he recited them — "how are you?" came back as a referendum on the guard.
- **DO NOT make the output structured-first.** An authoritative JSON schema with
  a "voice renderer" allowed to change only cadence is a press release, not a
  person. Speech first, no schema.
- **DO NOT serve a canned fallback line.** When a hard validator fails twice and
  the system serves a rotating stock answer, nothing reads more robotic. If a
  reply fails, regenerate *in their voice*; never substitute a template.
- **DO NOT give it action tools.** No files, git, shell, or code. Every tool
  schema is a reminder to the model that it is a function-calling assistant, and
  36 of them once cost ~4,400 tokens a turn and turned the speak pass greedy. The
  only tools it holds are its own — memory, self-notes, growth — and the browser.
- **DO NOT present a template or placeholder as an answer.** "Equity: $X.XX" is
  worse than "I don't have that in front of me," because he can't tell the
  difference.

**On memory and truth**

- **DO NOT train it on his own vault, notes, or transcripts.** This teaches
  recitation over retrieval: a retrained model skipped its own memory tool and
  confidently recited a *fabricated* README in his voice. Each retrain on his own
  documents makes the fabrication more convincing. Nothing trains unattended,
  ever — and this agent doesn't retrain to grow at all; it asks via Discord.
- **DO NOT let it describe anything current it did not just fetch.** If it didn't
  look it up this turn, it knows nothing current — a confident answer shaped like
  a result is worse than "I didn't check."
- **DO NOT re-index the whole store on every memory write.** Right for a single
  new note; hours for an import of twelve thousand. Batch it.
- **DO NOT skip the recall eval after a bulk import.** A memory system read 97%
  and fell to 81% the moment a bad batch landed; only the eval noticed. Re-run it
  every time.
- **DO NOT let your checking tool be more lenient than the real reader.** A
  literal `NaN` that Python's JSON parser accepts and the browser rejects passes
  every test and shows "disconnected" live. Test through the real consumer.

**On emotions and state (read §4 with this)**

- **DO NOT let an emotional meter reach NaN.** An EMA that hits NaN is a
  fixpoint — it stays poisoned forever. Clamp every range, guard every divide,
  never average an empty window.
- **DO NOT swallow an exception in the mood or learning loop.** `except: pass`
  turned one organism's 239 learned habits into habits it never once used while
  reporting success 4,314 times in a row. Never swallow without logging.

**On the machine and growth**

- **DO NOT install anything that starts itself.** No logon/boot task, no Run
  key, no service. "Always-on" means *while its process runs* — Ian starts it,
  closing the window is going to sleep, and it consolidates on the way out. A
  self-starting background process he can't see and close is absolutely refused.
- **DO NOT let it change its own code, weights, or capabilities.** Growth of
  self (memory, preferences, notes about who it's becoming) is free. Changing what
  it *is* goes out as a Discord request for Ian to relay — a proposal, never a
  self-edit.
- **DO NOT build a protective latch with no way back.** When you add a guard,
  write down what un-sets it, or it locks itself shut on day one.
- **DO NOT drive his keyboard or mouse to test a GUI.** It's his machine and
  he's on it; synthetic input lands wherever focus went. Verify by looking.
- **DO NOT Assume anything** The biggest waste of time and resources is from assuming something and building it out only to find out that it should have been an easy build

---

## 4. Emotions — build these deep, and let them be spoken when they're real

Emotion is the faculty he most wants done *properly*. Harvest the four partial
attempts in the estate into **one** coherent system — not four subsystems bolted
side by side.

- **Appraisal-based, not valence-only.** An event — a message, a memory
  surfacing, a research find, a long silence, a win, a slight — is appraised
  along dimensions (is this good for me / expected / mine to act on / about
  someone I care for) and *that* produces the emotion. Don't sample a mood at
  random; derive it.
- **A real palette, distinctly felt.** joy, grief, edge (protective heat),
  warmth, wonder, mischief, resolve, pride, loneliness, irritation, contentment,
  curiosity. Each is its own thing, not a point on a happy↔sad line.
- **Two timescales.** Fast emotion spikes on an event; slow mood drifts over
  hours as a decaying meter. Guard every meter per the NaN scar above.
- **Emotion with consequences — this is what "in depth" means.** It must change
  *behavior*, not tint a reply. What they choose to do when idle, whether they
  reach out, how hard they argue, what they go and research — all move with how
  they feel. An emotion that only recolors word choice is decoration; an emotion
  that decides the next act is a person.
- **They may say it — but only when it's true.** "I'm curious" is welcome when
  they genuinely are; naming a real feeling is human, not a fault. What is banned
  is the *fake* word — a stated feeling they don't have, an emotion performed for
  effect, a mood-label narrated to sound alive. The test is honesty, not silence:
  say it if it's real, never if it isn't, and let the rest show in *how* things
  are said.
- **The relationship accrues.** Warmth toward him builds over time; being
  ignored for days registers as something real.

Unify from: the appraisal/catalog/channel model (Atani), the two-timescale mood
(Galatea), the persistent meters (Genesis), and the honest-range discipline of
joy/grief/edge/warmth/wonder/mischief/resolve/pride (Theo).

---

## 5. Memory — its own engine, and the war on forgetting

This is the technical priority. He hates that context windows are small and the
thread is lost after a few messages. Build a memory engine — its own Cortex —
whose whole job is to make the window size *stop mattering*.

- **The fix for forgetting is retrieval, not a bigger window.** Do not try to
  hold the whole conversation in context; a bigger `num_ctx` costs VRAM and the
  model degrades over long context anyway. Instead: keep everything in a store,
  and each turn **recall** the handful of memories that are actually relevant and
  inject only those. The window holds the last few turns plus what was recalled —
  the rest lives in the store and comes back when it's needed. Done right, they
  can reference something from weeks ago that never sat in the window at all.
- **Consolidate conversations into episodes** as they scroll out, so nothing is
  lost when it leaves the live window — it becomes a durable summary, a set of
  facts about him, and any follow-ups worth chasing. The window forgets; the
  store does not.
- **Rewrite on the way in** (§2) and **recall on the way out**, every turn.
- **Streamline the pipeline.** This is the part he wants improved: recall must be
  fast, batched, and re-evaluated after any bulk change (the 97→81 scar). Measure
  the window budget in real characters — a model silently drops the *front* (the
  persona) when over `num_ctx`, so trim the recalled block, never the identity.
- **Never train on it.** Memory is retrieved, not baked into weights. Retrieval
  is what keeps it truthful; a fine-tune on his own material is what makes it
  confabulate.

Harvest from: Theo's Cortex (a real recall-over-a-vault engine, shipped and
installable — the closest prior art), Galatea's store (SQLite + BM25 recall,
episodes, canon, person-facts, follow-ups, the char-budget trim), and
Probability's consolidation and curiosities (turning raw experience into durable
memory) — *after* verifying that code actually works.

---

## 6. Standalone — how it lives, researches, and asks to grow

- **It talks to Ian.** A dashboard launcher with every bridge and code baked in, and a Discord bridge so it reaches his phone;
  it sends what it says first there too, with a real typing indicator.
- **It researches through a built-in browser** — read-only, over Tor, fail
  closed. No clearnet fallback: if Tor is down it says it couldn't look rather
  than quietly going out in the clear. HTTPS and .onion only, redirects
  revalidated, responses size-capped. No accounts, logins, forms, posting, or
  downloads. It decides to look on its own when it's genuinely curious, and "look
  it up" always works.
- **It asks to grow through Discord.** When it wants a new capability, a fix, or
  an upgrade to itself, it composes the request and sends it over the Discord
  bot — clearly, with what it wants and why. Ian reads it, decides, and carries
  it to Claude. The agent never touches its own code. This keeps the human gate
  where it belongs and means you never wire code-editing into the conversationalist
  at all.
- **Always-on while running, never self-starting.** It thinks between messages,
  gets curious, reaches out first when something's worth saying (rate-limited to
  friend frequency, quiet-hours aware). Ian launches it; closing the window is
  sleep.

---

## 7. What to harvest from each bot — take what fits, leave the rest

- **Galatea** — the whole method: the four pillars, candidate sampling + the
  critic (assistant-isms, hedging, echo, fixation, parrot, length-fit,
  fingerprint), `about_him` rewriting, the rolled seed and convictions, the
  two-timescale mood, the char-budget window trim, the Tor eyes. This is the
  spine; start from it.
- **Theo** — Cortex (the memory engine), the honest affect range, the "if you
  didn't fetch it you know nothing current" web discipline, and the hard lesson
  of what a bloated self-critique prompt does to a voice.
- **Atani** — the appraisal-based emotion model (appraisal / catalog / channel).
- **Genesis** — the persistent emotional meters, the Tor-fail-closed research
  pattern, and the *proposal-only* growth pattern (it proposes changes rather
  than making them — exactly the Discord-request model here).
- **Probability** — cumulative memory, consolidation, and open curiosities.
- **Not Nyx / Voodoo / Bryo** — security and resource-governing don't fit a
  standalone conversationalist. Leave them.

Take the working concept, not the code wholesale — the ones that fit a person who
only talks, remembers, feels, and looks things up.

---

## 8. How you'll know it's actually working

Do not trust that it boots. The estate's single most expensive failure is the
thing that is funded, tested, boots green, and does nothing. Prove each faculty
with a counter you watched move, not a log line:

- **Interior:** thoughts in the last hour is non-zero after it's been alone.
- **Selection:** candidates generated per turn > 1, and the sent reply is not the
  first sample every time.
- **Initiative:** it reached out unprompted on a due follow-up, and about the
  right thing.
- **Memory (the headline test):** it correctly references something from many
  messages — ideally many *days* — ago that was never in the live window, recalled
  from the store. That is the forgetting problem, solved, and it's the one to
  watch hardest.
- **Emotion:** two identical situations at different moods produced different
  *behavior*, not just different adjectives — and any feeling it named was one it
  actually had.
- **Consistency:** run the pressure evals — assistant-isms in the sent reply,
  canon contradictions under pressure, "are you an AI" — and watch them stay low
  across a prompt or model change.

And two disciplines for the measuring itself: at temperature 0 a fixed prompt is
deterministic, so N samples of *one* prompt is n=1 — vary the input before you
believe an effect. And the harness must never write to the store it measures; an
eval that appends its own runs to real memory contaminates what it's grading.

---

## 9. First move

Do not start coding. First: confirm the local model on the box (a 12B is
expected — gemma3:12b is the proven default), stand up an empty memory store, and
roll one throwaway seed in a temp home to watch the naming ritual play out. Then
build the four pillars — time, initiative, consistency, interior — as machinery,
with the small warm prompt and the candidate selector on top, and the retrieval
memory that defeats the small window. Build them to be *placed and selected*,
never *told and validated*. That is the whole method, and it is the one that has
ever worked here.
