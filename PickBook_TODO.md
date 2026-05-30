# PickBook TODO Progress

Last updated: 2026-07-10

## Current Localhost Status

- [x] Fixed the homepage loading loop.
- [x] Confirmed `http://127.0.0.1:8000/health` responds successfully.
- [x] Restarted localhost using the local SQLite development database.
- [x] Seeded the local database with compact book data so the library can load.
- [x] Restored Admin Tools to the PickBook chocolate/gold theme.
- [x] Made Admin Tools sections collapsible so the page feels less scattered.

## Cleanup

- [x] Removed generated Python cache folders from the PickBook workspace.
- [x] Removed stale zero-byte uvicorn log files.
- [x] Kept source files, migrations, database files, book SQL files, legal docs, and current server logs.

## Authentication And Accounts

- [x] Added password-backed signup and login.
- [x] Added logout and account delete actions.
- [x] Added account UI for sign in, create account, logout, and delete account.
- [x] Added account settings for password changes, subscription visibility, sync, logout, and account management.
- [x] Changed account deletion to warn clearly and delete all account-linked user data after typed confirmation.
- [ ] Add production email verification and password reset delivery.
- [ ] Add fuller reader settings and profile management.

## Author Upload Experience

- [x] Fixed the author upload layout enough for mobile use.
- [x] Made allowed manuscript types and 50MB max size more visible.
- [x] Added book cover upload endpoint, storage, validation, and UI.
- [x] Kept author agreement download visible during submission.
- [x] Kept required consent visible during submission.
- [x] Added ownership messaging during upload.
- [x] Added edit, export, unpublish, and delete controls for author stories.
- [ ] Add richer validation messages and final visual polish.

## Admin Review Workflow

- [x] Wired pending-review dashboard counts to the actual database.
- [x] Added approve and reject endpoints for submitted stories.
- [x] Added rejection reason and feedback support for authors.
- [x] Added pending story queue UI in Admin Tools.
- [x] Added richer admin story preview before approval.
- [x] Added filters for pending, rejected, published, and unpublished stories.
- [x] Added deeper audit trail/history for admin decisions.

## Reader Engagement

- [x] Added ratings.
- [x] Added likes.
- [x] Added comments/reviews.
- [x] Added database tables for reader feedback.
- [x] Added moderation behavior for comments/reviews.
- [x] Added reader display names on approved comments.
- [x] Added reader comment editing/deletion and abuse reporting.
- [x] Added admin moderation notes for comments/reports.
- [ ] Add richer review sorting, filtering, and reporting controls.

## Author Dashboard, Earnings, And Withdrawals

- [x] Modeled author story states more completely: draft, pending, completed, rejected, published, and unpublished.
- [x] Added per-author earnings ledger.
- [x] Added bank account fields for authors.
- [x] Added withdrawal request flow.
- [x] Added admin payout processing flow.
- [x] Connected premium reading, download, and tip revenue into the author earnings ledger.
- [x] Added minimum payout settings and payout history export.
- [x] Added payout receipts and clearer payout history.

## Author Ownership And Rights

- [x] Added clear ownership messaging in onboarding/upload/dashboard areas.
- [x] Emphasized: "Your stories remain yours. Always."
- [x] Added author story edit, export, unpublish, and delete controls.
- [x] Added ownership and deletion-policy copy to account and author upload surfaces.
- [ ] Add a dedicated rights/ownership help page or modal.

## Author Profiles And Follows

- [x] Added backend models for author follows.
- [x] Added public author profile API.
- [x] Added follow and unfollow endpoints.
- [x] Build the public author profile UI.
- [ ] Show follower counts and author story shelves in the main reader UI.

## Achievements And Reader Progress

- [x] Added backend model for reader achievements.
- [x] Added achievements API.
- [x] Added a basic achievements section in the Features view.
- [x] Added referral/subscription details to the Features view.
- [ ] Add richer achievement rules and unlock events.
- [ ] Add achievement badges to reader profile/account areas.

## Remaining Build Priorities

1. Add richer reader achievement unlock behavior.
2. Polish author upload validation and mobile alignment.
3. Add richer review sorting/filtering beyond the current newest/highest/lowest controls.
4. Add a dedicated rights/ownership help page or modal.
5. Run a full browser pass once localhost browser access is stable again.
