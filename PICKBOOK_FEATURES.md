# PickBook Features

This file summarizes the major features currently present in the PickBook codebase.

## Reader Experience

- Home library with recommended, trending, latest, and continue-reading sections.
- Search by novel, author, genre, and related text.
- Genre and mood filters for browsing.
- Novel detail pages with cover art, synopsis, tags, rating display, chapter list, and reader reactions.
- Chapter reader with progress bar, next/previous chapter navigation, font-size controls, sharing, bookmarks, and related recommendations.
- Reading progress tracking across books and chapters.
- My Shelf for saved novels.
- Downloaded Novels view for offline-access tracking.
- Reader streak tracking, streak calendar, and achievement badges.
- Taste picker onboarding for personalized recommendations.
- Age gate for adult content.
- Light/dark theme switching.
- Mobile menu and responsive reader UI.

## Accounts And Reader Sync

- Account signup, login, and logout.
- Email verification request and verification callback.
- Password change.
- Password reset request and reset confirmation.
- Profile preferences including display name and notification preferences.
- Account deletion.
- Server-backed reader-state sync for shelf, progress, bookmarks, downloads, follows, and achievements.
- Local-only fallback state for unsigned-in readers.
- Session refresh through `/api/me`.

## Subscription, Payments, And Monetization

- Standard subscription checkout through Paystack.
- Checkout verification callback.
- Coupon validation and coupon-based access boosts.
- Referral claim flow.
- Donation checkout.
- Subscription plan display and upgrade prompts.
- Premium-read tracking for author earnings.
- Paystack webhook handling.
- Pending payment tracking.

## Reader Engagement

- Likes and comments on novels.
- Reader rating and short review/comment submission.
- Comment editing and deletion for the current reader.
- Comment reporting.
- Moderation status support for reader comments.
- Public engagement summaries on novel detail pages.

## Announcements

- Reader-facing announcements feed.
- Announcement popup support.
- Read and dismissed announcement state.
- Audience-aware announcement delivery.
- Deep-link support from announcements.
- Priority, scheduling, and expiration fields in the data model.

## Author Studio

- Author application and onboarding flow.
- Pen name, genres, bio, and writing-sample collection.
- Admin approval or rejection of author applications.
- Story/profile creation for authors.
- Manuscript upload support for `.txt`, `.docx`, and `.epub`.
- Cover upload, cover preview, cover replacement, and cover removal.
- Draft and submission dashboard.
- Story metadata editing.
- Story export.
- Author-controlled unpublish.
- Author story deletion.
- Author bank-details management.
- Author withdrawal requests.
- Author notifications.
- Author earnings summary.
- Premium reader analytics panel.
- Public author profile pages.
- Author follow/unfollow.
- Author story shelf.

## Admin Console

- Token-protected admin tools page.
- Dashboard metrics for stories, authors, comments, withdrawals, coupons, and premium activity.
- Announcement creation, editing, listing, deletion, and delete-all controls.
- Author application review with approve/reject actions.
- Story review queue for pending submissions.
- Story approval and rejection with review feedback.
- Recent and searchable story lists.
- Story audit history.
- Pending comment moderation.
- Comment approve/reject moderation actions.
- Premium analytics overview.
- Withdrawal review and payout decision flow.
- Payout settings management.
- Withdrawal export.
- Coupon creation, listing, and deletion.
- Accounting summary for revenue and payout estimates.

## Deals, Partners, And Investors

- Partner registration.
- Investor registration.
- Investor payment checkout.
- Partner and investor agreement/document uploads.
- Deals summary dashboard.
- Partner referral-code tracking.
- ROI and payout calculator UI.
- Static agreement documents for authors, marketing partners, and micro-investors.

## Catalogue And Content Data

- Book catalogue API with filters and limits.
- Chapter API for stored or generated chapter content.
- Search index model for book and story discovery.
- Story versions for review, publication, and scheduling workflows.
- Draft storage.
- Reading progress, bookmarks, library items, and download records.
- Reader engagement, premium reads, review audit records, author earnings, withdrawals, follows, and achievements.

## PWA And Notifications

- Web app manifest.
- Service worker.
- Push subscription manager.
- Separate push API service using Supabase and web-push.
- User push subscription support for signed-in users.
- Notification preference storage.

## Security And Operations

- Admin-token protected backend routes.
- Header-based auth/session token handling for reader and author APIs.
- Rate limiter support.
- Database migrations through Alembic.
- Startup schema patching for existing deployments.
- Railway deployment configuration.
- Docker Compose support for local Postgres and Redis.
- PostgreSQL and SQLite development fallback support.
- Storage abstraction for uploaded files and documents.
- Plausible analytics integration module.

