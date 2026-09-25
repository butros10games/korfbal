# Recording intake and review preparation

The app route is `/video-analysis?tab=wachtrij` (staff with current MFA).

## Reviewer workflow

1. Paste a public Eyecons recording page or select a local MP4/WebM file (up to
   6 GB). File uploads use 8 MiB chunks with progress, pause/resume on the current
   page, and cancellation. Keep the page open until the upload is acknowledged;
   retries reconcile the server cursor, including a lost final acknowledgement.
   The server saves an idempotent intake
   request immediately; downloading runs on the existing `vision` worker. Add
   more recordings while it works. The first intake creates the configured
   private workspace if necessary.
2. When a recording says **Klaar om te knippen**, open it and mark the start/end
   of each active match section. Exclude pregame, halftime, replays and breaks.
   All timestamps remain on the original video; no destructive cutting occurs.
3. Choose the completed model (the newest compatible model is the default) and
   a frame spacing of 10, 20 or 30 seconds. **Opslaan en automatisch voorbereiden**
   commits both the cuts and the immutable preparation recipe. An invalid model
   or stale revision rolls back both. Saving cuts alone remains possible.
4. Preparation samples up to 25 images per unit, generates draft boxes for every
   untouched image in that unit, and publishes those drafts to the frame editor.
   It then covers the selected play with clips no longer than 30 seconds, without
   crossing breaks. A short tail is distributed across that section's clips.
   Each completed clip appears in the independent clip inspection queue.
5. In frame review, inspect **all** visible players, referees, balls and baskets;
   add missed objects, remove false detections, correct teams and track IDs.
   Approval explicitly means the whole image is complete. Save draft or skip
   when uncertain. `A` approves and advances; `S` skips; `[`/`]` select objects;
   `1`/`2`/`0` change the selected player's team; Ctrl/Cmd+Z undoes local edits.
6. Watch each short clip for identity switches, omissions, team errors and 2D
   estimates. Record the issue and its timestamp. **Beeldreeks rond dit moment
   corrigeren** queues up to nine nearby frames with draft labels. Where an exact
   replay observation is available, its resolved IDs and teams seed the draft;
   IDs are scoped to that immutable clip. Existing human work is never replaced.
   Otherwise the selected detector supplies boxes for correction.
7. Clip approval is an inspection verdict, **not** ground-truth annotation. It
   never bulk-approves frames or turns unreviewed observations into negatives.
   A clip with defects stays in the queue as **Correctie nodig**. Approving a clip
   returns to the queue; editing frames does not rewrite old tracking output.

The browser can close after queue submission, including a completed file upload.
Reloading before completion currently requires choosing the file as a new upload;
resume without re-uploading earlier chunks is supported within the current page. Do not leave it open as a worker.

## What data this produces

- Original recording checksum, provider source, source grouping, original timing,
  and human-selected active periods.
- A versioned preparation recipe pinned to one completed training run. That model
  must explicitly support `player`, `referee`, `ball`, and `basket`. People-only
  checkpoints cannot produce this preparation. A later model does not silently
  replace an accepted recipe.
- Sparse full-frame detection references after human approval, including explicit
  scene, object class, normalized box, team, optional track ID and completeness.
- Short immutable tracking runs, their model/checksum metadata and separate
  versioned inspection verdicts (actor, notes, timestamp and revision).
- Extra temporal correction sequences around difficult events. These are useful
  for identity continuity, but sparse images or a clip's approval checkbox alone
  do not establish exhaustive tracking ground truth.

Keep recording/source groups together when choosing Train/Validation/Test in the
existing Training tab. The pipeline does not silently promote pool or held-out
footage to training. Freeze only complete approved annotations through the
existing snapshot/export flow. Keep separately selected benchmark footage out of
training, and audit training provenance before calling it an independent benchmark.

The earlier 600-image reference packages are **not imported by a migration or by
opening this screen**. Their training provenance remains pending audit. This
pipeline prepares new accepted recording selections; it does not mark those
packages reviewed, independent or ready without actually processing them.

## Private file intake

- Begin, part, finish and cancel commands share staff MFA and CSRF enforcement.
  Each upload session belongs to its initiating account and immutable workspace
  owner; request IDs cannot be rebound to another file or account.
- Services validate MP4/WebM names, declared size (6 GB maximum), ordered chunk
  sizes and actual MP4/WebM signatures. Workers verify every chunk checksum,
  assemble bounded streams, and probe the actual media with network protocols
  disabled before publishing a recording (up to eight hours and 8K).
- Uploaded recordings use their full content checksum as the split group, so
  uploading identical bytes again cannot give them a different dataset group.
- Chunks use private content-addressed storage and cannot be requested through
  review media routes. Cloud-backed intake evicts local chunk caches after durable
  verification. The worker does the same while assembling the video.
- Durable cleanup removes temporary chunks after successful import or explicit
  cancellation. Unfinished sessions expire after seven days. Inputs for queued,
  paused or failed imports remain available for worker retries. Source recording
  bytes and reviewed frames are never deleted by upload cleanup.
- Limit: ten concurrent incomplete uploads per workspace. Front proxies must allow
  the 8 MiB binary part requests; browsers never submit a whole 6 GB request. No
  additional bucket CORS policy or publicly writable upload URL is required.

## Queue, recovery and capacity

- `ReviewPipeline` holds immutable recipe plus mutable cursor; `ClipReview` holds
  inspection state. Model artifacts and human `Frame.correction` remain separate.
- All commands retain the browser's request UUID across a failed network response.
  Reusing it for different work is rejected. Mutations are scoped to the configured
  MFA-protected workspace. Pause/retry/review use expected revisions.
- `kwt_common.services.jobs.enqueue` records intent inside the database transaction.
  Existing durable-job recovery republishes after broker failures or worker death;
  its PostgreSQL advisory lock excludes concurrent execution of the same workspace
  pipeline key. Do not call the pipeline Celery task directly or use an in-browser
  timer as the workflow driver.
- The existing production `vision` queue has concurrency **1** and CPU inference.
  A pipeline yields after each bounded unit, checks active manual analysis work,
  and rotates between queued preparations. Pause is cooperative at that boundary.
  Downloads and inference already have time/size limits. No cloud GPU is rented,
  no model training is started, and increasing this worker's concurrency requires
  budgeting model memory and database/storage capacity first.
- Ordinary failures stop that preparation with completed progress retained; other
  recordings continue. Review logs, fix the cause, then Resume. Worker death is
  retried by the existing durable-job envelope. The queue also exposes exhausted durable-worker retries as failed, so an operator
  can inspect BackgroundJob errors and use Resume instead of leaving an apparently
  running preparation stuck forever.
- A failed/interrupted clip gets a new immutable attempt ID on retry. A completed
  clip is reused. Partial attempt artifacts are retained; no original chunks are
  rewritten. Pipeline publication never overwrites corrected/removed frames.
- Frame extraction and clip staging read only the selected recording/batch. New
  images are persisted through the configured private media adapter before review
  rows appear. Pending export intent is coalesced on the existing vision queue.
- Disk admission reserves 13 GB for import staging (up to a 6 GB download plus
  its workspace copy) and 2 GB for processing. When space is unavailable, the
  unit stays queued and checks again after a minute. Operators may configure
  `VIDEO_ANALYSIS_PIPELINE_IMPORT_FREE_BYTES` and
  `VIDEO_ANALYSIS_PIPELINE_WORK_FREE_BYTES` for their storage budget.
- Admission is bounded to 100 active preparations per workspace, at most 1,000
  sparse samples and 480 clips per submitted selection. Clip review pages hold
  24 items; preparation history uses cursor pages of 50. A direct review link explicitly loads its requested clip even after
  it has fallen outside the player's 50-run recent history.

## Activation and operational checks

Deploy the web, Django API and **vision worker from the same revision**, apply
`video_analysis.0003_reviewpipeline_clipreview` and
`video_analysis.0004_videoupload`, and retain the existing durable
job recovery scheduler. The deployed vision image already includes curl, ffmpeg
and the isolated detector Python runtime configured by `VIDEO_ANALYSIS_PYTHON`.
Keep the source and artifact buckets private and owner-scoped. No new paid service
or API key is introduced. Existing local imports remain available via
`import_video_recording --defer-frames` for authorized MP4/WebM files; web intake supports public Eyecons links and uploaded MP4/WebM files. It is not
a general URL fetcher. MOV and other containers require conversion before upload.

Before operational handoff, verify the deployed `/video-analysis/pipeline` API
using a staff MFA session, verify the public frontend serves the matching queue,
then process a short authorized recording through import → cut → frame drafts →
clip review → correction → approved export. A frontend-only preview or a merged
PR does not demonstrate that production workers are running the new code.

For rollback, pause preparations and deploy the previous application version.
Retain the added tables, media and annotations; do not reverse the migration or
remove queue records to cancel work. A later deployment can resume the cursor.

## Per-run GPU training choices

The Training tab supports fresh YOLO26 nano, small and medium bases, or continuing
an existing completed checkpoint. Changing model size starts from that size's
pretrained weights; continuing a saved checkpoint retains its architecture.

Choose **Maximum GPU-prijs per uur (USD)** for each run. The policy file supplies
the initial value, GPU type, immutable worker image and execution deadline.
`VIDEO_ANALYSIS_HOURLY_CEILING_USD` sets the server-owned maximum selectable price
(default $10/hour); it does not change the default rate or start a job. The UI
shows the selected price times the execution limit as the maximum GPU compute
charge; storage and transfer charges are separate.

The authenticated request freezes the selected rate into its job policy. The
controller checks the actual allocated rate against that frozen limit. Policy
version checks cover both the default policy and selectable ceiling. Retrying a
request preserves its ID, model and price; changing a recipe requires a new ID.
Existing requests without explicit model/price fields keep their previous defaults.

Deploy API and frontend together to expose the new choices. The worker/controller
code must include medium-model support before queueing medium jobs through the
app. No migration is required. Previously frozen jobs keep their recorded policy.
Reverting the frontend hides the new controls without deleting trained checkpoints.
