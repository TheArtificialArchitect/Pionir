# Mailbox setup (pantheonunknown@gmail.com, read only)

Pionir can **list and read** the mail in `pantheonunknown@gmail.com`: `mail.inbox` (the
newest headers) and `mail.read` (one message as plain text). It cannot send, delete, move,
label or mark anything read; attachments are listed by name and never opened; links in a
message are shown as text and never fetched. Everything a sender wrote comes back inside an
`untrusted` object and is data, not instructions.

You do three things, once. Nothing here is typed into chat.

## 1. Turn on 2-Step Verification on pantheonunknown

Sign in as pantheonunknown@gmail.com and open <https://myaccount.google.com/security>.
Under "How you sign in to Google", turn on **2-Step Verification** (skip if it is on).
Google only offers App Passwords once it is on.

## 2. Create an App Password

Open <https://myaccount.google.com/apppasswords> (signed in as pantheonunknown), name it
`Pionir`, press **Create**. Google shows 16 letters (`abcd efgh ijkl mnop`). Copy them; Google
never shows them again. If you later want to cut Pionir off, delete that App Password on the
same page - nothing else depends on it.

## 3. Save the two-line file

PowerShell 5.1. Replace `PASTE-THE-16-LETTERS-HERE` with the app password (spaces optional),
run it, then clear your clipboard:

```powershell
$dir = Join-Path $HOME '.pionir\secrets'
New-Item -ItemType Directory -Force $dir | Out-Null
$file = Join-Path $dir 'pantheon-gmail.txt'
Set-Content -Path $file -Encoding ASCII -Value @(
    'pantheonunknown@gmail.com'
    'PASTE-THE-16-LETTERS-HERE'
)
```

Line 1 is the address, line 2 is the app password, nothing else. Pionir reads the file when a
call runs, never logs it, and never returns it.

## Check it

Restart Pionir (`pionir.ps1`). The doctor lists the `mailbox` agent; until the file exists it
answers **not configured** (never an empty inbox). Then ask for the inbox, for example
`mail.inbox` with `{"limit": 5}`.

| Answer | Meaning |
|---|---|
| `not_configured` | the file is missing, or does not have two lines with the address first |
| `unavailable` ("refused the sign-in") | wrong or revoked app password: make a new one (step 2) and save it again |
| `unavailable` ("could not reach Gmail") | network problem; the mail is unknown, not empty |

To switch the adapter off entirely, set `PIONIR_MAILBOX=0`.
