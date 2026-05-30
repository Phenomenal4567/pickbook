# PickBook Build Status

Last updated: 2026-07-10

## What I Finished

### Auth and account foundation

- Added password-backed reader signup and login.
- Added secure password hashing with `pbkdf2_sha256`.
- Added a `password_hash` field to `profiles`.
- Added an Alembic migration for `profiles.password_hash`.
- Added startup schema patching so existing local databases get `password_hash` automatically.
- Changed session refresh so the app no longer logs users in just because an email is stored locally.
- Added `/api/auth/logout`.
- Added `/api/auth/account` delete endpoint.
- Updated `/api/me` to return public account details needed by the UI.

### Reader account UI

- Updated the account modal with Create and Sign In modes.
- Added a password field to account creation and sign-in.
- Added a signed-in account panel.
- Added an Account Settings page for subscription visibility, password changes, sync, logout, and account management.
- Added typed account-deletion confirmation that tells users all account-linked data will be permanently deleted.
- Added visible buttons for:
  - Sync Now
  - Log Out
  - Delete Account
- Added `/api/auth/password` for authenticated password changes.
- Changed `/api/auth/account` deletion to hard-delete account-linked reader, author, follow, achievement, payout, payment, and authored catalogue data.
- Updated reader-state sync responses to include email and email verification status.

### Verification done

- Ran Python compilation check: `python -m compileall app`.
- Smoke-tested auth endpoints:
  - signup
  - login
  - `/api/me`
  - logout
  - delete account
- Smoke-tested password change and full account deletion with a temporary local user, author story, catalogue book, follow, achievement, payment, payout, and reader activity fixture.
- Opened the local site in the in-app browser.
- Verified the account modal shows Create, Sign In, and Password controls.
- Verified the signed-in account panel shows Log Out and Delete Account controls.

### Author upload experience

- Fixed the author dashboard layout behavior for mobile by making author status rows collapse cleanly.
- Added prominent manuscript and cover upload rules in Author Studio.
- Added cover upload support for author submissions.
- Added server-side cover validation for JPG, PNG, and WEBP files up to 5 MB.
- Added cover storage under `public/uploads/covers`.
- Mounted `/uploads` so stored covers render in the browser.
- Kept the author agreement download and required consent visible at submission time.
- Added author ownership messaging:
  - "Your stories remain yours. Always."
  - PickBook receives only a non-exclusive hosting/distribution license.
- Added author story actions:
  - edit
  - export
  - unpublish
  - delete

### Admin review workflow

- Replaced the hardcoded pending review count with real database counts.
- Added pending story review list.
- Added admin approve endpoint.
- Added admin reject endpoint.
- Added rejection/feedback field for authors.
- Approved author stories are published into the reader catalog.
- Added admin comment moderation endpoints and UI.
- Added admin withdrawal list and manual payout processing.
- Made Admin Tools sections collapsible.
- Restyled Admin Tools into a cleaner dashboard-style console.
- Added story status filters for pending, rejected, published, unpublished, and all stories.
- Added manuscript preview details before approval.
- Added story review audit rows and an Admin Tools audit viewer.
- Added author notifications for approval, rejection, resubmission, unpublish, and delete events.

### Reader engagement

- Added reader engagement table.
- Added ratings.
- Added likes.
- Added comments/reviews.
- Comments are hidden until approved by an admin.
- Added reader-facing reactions/reviews UI on the book detail page.
- Approved comments now show reader display names instead of internal IDs.
- Readers can edit/delete their own reviews and report abusive comments.
- Admin moderation supports reported comments and moderation notes.

### Public author profiles

- Finished the public author profile UI path.
- Added viewer-aware follow state to author profile API responses.
- Added catalogue book IDs to author profile story shelves.
- Made published author story cards open the normal PickBook detail/reader flow when the story is available in the catalogue.

### Author dashboard, earnings, and withdrawals

- Added author earnings ledger table.
- Added withdrawal request table.
- Added bank details fields on author profiles.
- Added author-facing bank details form.
- Added author-facing withdrawal request flow.
- Added admin-facing mark-as-paid/reject payout processing.
- Connected premium reading, premium download, and successful author-targeted tip/donation payments into the author earnings ledger.
- Added configurable minimum payout settings.
- Added payout receipt IDs and withdrawal history export.
- Added author notifications to the dashboard response.

### Second-slice verification done

- Ran Python compilation check: `python -m compileall app`.
- Fixed a homepage loading-loop JavaScript parse error in the Author Studio action buttons.
- Verified the inline JavaScript for the homepage and Admin Tools with `node --check`.
- Seeded local SQLite with `books.compact.sql` so localhost has catalogue data to render.
- Opened Admin Tools in the in-app browser.
- Verified collapsible Admin Tools sections are generated and collapsed by default.
- Smoke-tested:
  - author submission with cover upload
  - admin pending story queue
  - admin story approval
  - reader rating/like/comment
  - admin comment moderation
  - author bank details
  - author withdrawal request
  - admin withdrawal payout processing
- Smoke-tested public author profile API with a temporary local author/story/book fixture, including follower count, viewer follow state, catalog count, and linked catalogue book ID.
- Smoke-tested story filters/previews, approval audit, comment approval/display names/report/edit/delete, payout minimum settings, author notifications, minimum-withdrawal enforcement, and author-targeted tip ledger posting with temporary local fixtures.
- Removed smoke-test profile, story, book, earnings, and withdrawal rows from the local SQLite database after verification.

## Localhost Status

- Local app URL: `http://127.0.0.1:8000/`
- Health check URL: `http://127.0.0.1:8000/health`
- Latest health check result: OK
- Current local verification was run with SQLite at `pickbook_local.db`.

## Important Notes

- The workspace already had many modified files before/around this build. I did not revert unrelated changes.
- The `.env` currently has `APP_ENV=production` and a remote Railway/Postgres `DATABASE_URL`.
- For local testing, the server was started with development settings and SQLite so localhost can work reliably without waiting on the remote database.
- Existing accounts created before this change may not have a password hash. Those accounts will need a reset/migration path before password login works.

## What Is Left To Do

### Auth and accounts

- Add a password reset flow for older/passwordless accounts.
- Add a real email sending provider for verification links.
- Add richer profile preferences such as display name editing and notification controls.

### Author upload experience

- Add richer cover management, such as replace/remove cover controls and cover previews in admin review.
- Add stronger upload retry/progress states for large manuscripts.

### Admin review workflow

- Add bulk review actions and richer search across reviewed stories.

### Reader engagement

- Add richer review filtering/report queues beyond the current sort/report/moderation controls.

### Author dashboard, earnings, and withdrawals

- Continue tuning revenue-share amounts once the business rules are final.
- Add real external author notification delivery.

### Author ownership and rights

- Add a dedicated ownership/rights help page or modal.
- Expand legal retention copy into the dedicated ownership/rights page.

### Enhanced author profiles

- Add richer public author portfolio metadata such as verified author badges, reads, tips, genres, schedule, links, achievements, and stats.
- Add follow/notification flow for readers.

### Reader achievements and gamification

- Add badge tables and award rules.
- Track achievement progress.
- Display earned badges on reader profiles.

### Final polish

- Improve referral code visibility.
- Improve subscription expiry/renewal visibility.
- Add stronger loading, error, and retry states across upload, submission, review, and account flows.
