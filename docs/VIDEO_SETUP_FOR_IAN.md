# Video pipeline: what only you can do

Everything below needs your Google account, your channel or your money-adjacent decisions, so
none of it was done for you. Nothing here is urgent: the pipeline builds videos and staging pages
without any of it. The uploader stays disabled until steps 1-6 are done **and** you mark a niche
`"live": true`.

## 1. Make the channel (one per niche, or start with one)

1. In YouTube, create the channel (a Brand Account is best, so the channel is not tied to your
   personal name). Fully faceless is fine.
2. Name and describe it honestly. Say in the About text that the videos use a synthetic voice and
   are made from cited public sources.
3. Verify the channel (phone) so uploads over 15 minutes and custom thumbnails work:
   YouTube Studio > Settings > Channel > Feature eligibility.

## 2. Google Cloud project (free)

1. Go to https://console.cloud.google.com and create a project, e.g. `pionir-video`.
2. APIs & Services > Library > search **YouTube Data API v3** > Enable.

## 3. OAuth consent screen

1. APIs & Services > OAuth consent screen. User type **External**.
2. App name `Pionir video`, your email for support and developer contact.
3. Scopes: add only `.../auth/youtube.upload`.
4. Add your Google account as a test user, **then click "Publish app" so the status is "In
   production"**. While an External app is in "Testing", Google expires its refresh token after
   **7 days**, and the uploader would stop working weekly. (An app that is only used by you does
   not need Google's verification for this; the sign-in screen will warn "unverified app" and you
   click Advanced > continue.)

## 4. Desktop OAuth client, and the secrets file

1. APIs & Services > Credentials > Create credentials > OAuth client ID > **Desktop app**.
2. Copy the client id and secret into this file (create the folder if needed):
   `C:\Users\Ian\.pionir\secrets\youtube-client.json`

   ```json
   {"client_id": "....apps.googleusercontent.com", "client_secret": "...."}
   ```

3. Do **not** paste either value into chat, a commit, or an issue.

## 5. Consent once

In a PowerShell window you can see, from `C:\src\Pionir`:

```powershell
powershell -ExecutionPolicy Bypass -File tools\video-youtube-consent.ps1
```

Your browser opens Google; sign in as the channel's owner and approve "Upload videos to your
YouTube account". The script writes `youtube-token.json` beside the client file. It prints no
token, installs nothing, schedules nothing, and ends when you are done. Read it first if you like:
it is short.

## 6. Know the limits you are accepting

- **Every upload is Private.** An API project that Google has not audited can upload only as
  Private. You open YouTube Studio, watch it, and click **Public** yourself. Then run
  `python -m pionir video published <video id>` so the staging pages and the sponsor page know it
  is public. (Applying for Google's audit later lifts this; it is not needed to start.)
- Quota: the default is 10,000 units a day and one upload costs 1,600, so six a day at most. One a
  week per channel uses almost none of it.
- Captions are not uploaded by the API (it would need a wider permission). Upload the `.srt` in
  the package folder through Studio if you want them.
- Revoke any time at https://myaccount.google.com/permissions (remove "Pionir video"), and delete
  `youtube-token.json`.

## 7. Turn a niche on

Only after the channel exists and the token is saved: in `src/pionir/video/niches.json` set that
niche's `"live": true` (the three shipped niches are all `false`, and an `"example": true` niche
can never be live). A live niche's finished videos are then parked as `video.youtube_upload`
cards. Approving a card uploads it Private; nothing is ever uploaded without that approval.

## 8. Sponsor page inputs (optional, whenever)

Files in `C:\Users\Ian\.pionir\video\` (all optional; the page is built into the staging
`site/sponsor/` folder and never deployed by itself):

- `sponsor.json`, for example:

  ```json
  {"organization": "Dokaz Industries",
   "url": "https://dokazindustries.com",
   "contact_email": "pantheonunknown@gmail.com",
   "packages": [{"name": "A mention in one video", "description": "...", "price": "$X"}]}
  ```

  With no `contact_email` or `form_action` (an https URL), the page says enquiries are not open.
  With no `price`, the page says prices are agreed by email. Nothing is invented and nothing is
  replied to automatically.
- `analytics.json`, only from real YouTube Studio numbers and only once a video is public:

  ```json
  {"as_of": "2026-12-01", "source": "YouTube Studio, last 28 days",
   "views_28d": 0, "subscribers": 0, "watch_hours": 0, "videos_measured": 0}
  ```

  Until this exists (or if it is more than 45 days old), the page shows "the channel is too new,
  there is no audience data yet" and no numbers.

## 9. Then

Run `python -m pionir video site`, read `~/.pionir/video/site/` in a browser, and deploy by hand
only when you are happy with it. Deploying pages is yours to do; nothing in the pipeline does it.
