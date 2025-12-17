# GDPR Compliance Report — Korfbal (Django backend + Korfbal Web Tool frontend)

**Date:** 2025-12-17
**Scope:**

- Backend: `apps/django_projects/korfbal` (Django/DRF + bg_auth)
- Frontend: `apps/node_projects/frontend/korfbal-web` (React + Vite + PWA)

> This is a technical compliance/readiness report (engineering view). It is **not legal advice**.

---

## 1) Executive summary

The Korfbal application processes personal data (user account details, profile picture uploads, and user-specific preferences). The architecture is generally security-minded (cookie-based sessions + CSRF, origin-restricted CORS).

During this remediation cycle, the project implemented **privacy controls** (profile picture + stats visibility based on club connection) and removed accidental PII leaks from several legacy endpoints.

Two previously high-impact technical risks were addressed in code:

1. **User-uploaded media is now configured as private by default** (signed URLs).
2. The **PWA Service Worker no longer caches `/api/player/...`** responses.

Remaining work is primarily **operational + product/legal**: confirm bucket policies match the private-by-default intent, add privacy/cookie policy pages, and add DSAR (export/delete/anonymize) workflows.

> Note: A `.env` file containing credentials exists in the repo. This is a security incident waiting to happen and should be addressed immediately (rotate secrets + remove from version control).

---

## 2) Data processing inventory (what personal data exists)

### 2.1 Identifiers and account data

**Source:** Django auth user model exposed through the player API.

- Django/DRF returns (at least):
    - `username`
    - _Potentially_ `email`, `first_name`, `last_name` when the viewer is the owner/admin.
    - For non-self views, the implementation was updated to **avoid leaking email and full names**.

**Evidence:** `apps/django_projects/korfbal/apps/player/api/serializers.py` → `UserSerializer`.

### 2.2 Profile image

- `Player.profile_picture` is an `ImageField` stored under `profile_pictures/`.

**Evidence:** `apps/django_projects/korfbal/apps/player/models/player.py`.

### 2.3 User preferences / usage data

- Club/team follows:
    - `team_follow` (Many-to-many)
    - `club_follow` (Many-to-many)
- Goal-song settings:
    - `goal_song_song_ids` (JSON list)

These are user-specific and can be considered personal data (relates to an identifiable user).

### 2.4 Spotify integration

The player module contains Spotify integration logic and requests Spotify scopes including `user-read-email`.

**Evidence:** `apps/django_projects/korfbal/apps/player/api/views.py` contains Spotify scopes.

**Implication:** If Spotify is used, you may become responsible for describing:

- what Spotify data is accessed,
- how long it is stored,
- how to disconnect.

### 2.5 Cookies and session identifiers

- Frontend uses cookie-based auth (`credentials: 'include'`) and reads CSRF cookie `csrftoken`.

**Evidence:** `apps/node_projects/frontend/korfbal-web/src/api/client.ts`.

---

## 3) Data flows (how data moves)

### 3.1 Authentication and session

- Browser uses **session cookies** to authenticate with the API.
- CSRF protection is implemented by reading `csrftoken` and sending `X-CSRFToken` for unsafe methods.

**Evidence:** `fetchJson()` uses `credentials: 'include'` and `withCsrfHeader()` reads `csrftoken`.

### 3.2 Profile updates

- Frontend updates account details (username/email) and password.

**Evidence:** `apps/node_projects/frontend/korfbal-web/src/pages/Profile/ProfileSettings.tsx`.

### 3.3 Profile picture upload

- Backend accepts a file upload and stores it under the configured default storage.

**Evidence:** `apps/django_projects/korfbal/apps/player/api/views.py` has profile picture upload handling.

### 3.4 PWA caching

- The frontend builds a PWA service worker (vite-plugin-pwa/workbox) that caches certain API routes.

**Evidence:** `apps/node_projects/frontend/korfbal-web/vite.config.ts`.

---

## 4) Current controls (what is already good)

### 4.1 Cookie-based session auth (good baseline)

No evidence of storing access tokens in `localStorage`/`sessionStorage`. Cookie-based sessions reduce XSS token-theft risk compared to storing JWTs in web storage.

### 4.2 Environment-aware security defaults

In production (when not DEBUG), the Django settings default to:

- `SECURE_SSL_REDIRECT = True`
- `SESSION_COOKIE_SECURE = True`
- `CSRF_COOKIE_SECURE = True`
- HSTS enabled with long duration

**Evidence:** `apps/django_projects/korfbal/korfbal/settings.py`.

### 4.3 CORS scoped to specific origins

CORS is configured to allow only specific frontend origins and uses credentials.

**Evidence:** `CORS_ALLOWED_ORIGINS`, `CORS_ALLOW_CREDENTIALS=True` in `settings.py`.

---

## 5) Findings & GDPR impact assessment

### Finding A (HIGH): User media appears publicly readable

**What (previously):** The storage config set `default_acl: "public-read"` for the default storage.

**Evidence (historical):** `apps/django_projects/korfbal/korfbal/settings.py` previously configured `STORAGES["default"]["OPTIONS"]["default_acl"] = "public-read"`.

**Why it matters (GDPR):** Profile pictures are personal data. Public-read storage means that anyone with the URL (or who can guess it, depending on storage config) may access the image without authentication.

**Risk:** Unintended disclosure, higher breach surface, potential special-category inference risk (a photo can reveal biometric/ethnic attributes, etc.).

**Status:** Implemented in code.

**Implementation:** Media storage now uses:

- `default_acl: "private"`
- `querystring_auth: True` (signed URLs)

**Remaining operational requirement:** Ensure the underlying bucket policy / ingress does not override this (e.g. a public bucket policy would still expose objects).

---

### Finding B (HIGH): PWA caches player API responses (PII persisted locally)

**What (previously):** Service worker cached `/api/(club|team|player)/` using `StaleWhileRevalidate` up to 7 days.

**Evidence (historical):** `apps/node_projects/frontend/korfbal-web/vite.config.ts` previously had a runtimeCaching rule for `/api/(club|team|player)/`.

**Why it matters (GDPR):** The `/api/player/...` response includes `user.email`, `first_name`, `last_name`, etc. This can persist in browser cache storage beyond the session.

**Risk:** Personal data persists on shared devices; unclear retention; harder to satisfy deletion in practice without cache invalidation.

**Status:** Implemented in code.

**Implementation:** `/api/player/` is now `NetworkOnly` in the service worker, and the semi-static cache allowlist was reduced to `/api/(club|team)/`.

**Remaining operational requirement:** Consider prompting users to refresh/update the PWA to ensure old caches are evicted (Workbox cleanup is enabled, but existing caches may persist until the updated SW activates).

---

### Finding C (MEDIUM): No explicit DSAR (export/delete) workflows surfaced

**What:** No obvious “download my data” or “delete my account” flows found in UI/API.

**Why it matters (GDPR):** Data subjects have rights (access, erasure, portability). Even if you handle requests manually, you should document the process and ideally provide tooling.

**Recommendation:** Add admin tools and/or user-facing endpoints to export and delete/anonymize user data (including profile images, follows, and associated objects).

---

### Finding D (MEDIUM): Privacy/cookie disclosures not clearly present in the frontend

**What:** UI contains a “Beveiliging & privacy” section label, but no visible privacy policy/cookie policy pages were identified in the code scan.

**Why it matters (GDPR/ePrivacy):** Transparency requirements: you must inform users about processing, cookies (even if strictly necessary), retention, and contact info.

**Recommendation:** Add Privacy Policy + Cookie Policy routes and link them in the UI footer/settings.

---

## 6) Recommended remediation plan (prioritized)

### P0 — Fix immediately

1. **Make user uploads private** ✅

- Implemented: default media storage is private + signed URLs.
- Still required: verify bucket policy/ingress isn’t public.

2. **Stop caching user/PII endpoints in the PWA service worker** ✅

- Implemented: `/api/player/` → NetworkOnly.

### P1 — Next sprint

3. **Add privacy/cookie policy pages** (frontend)
4. **Add DSAR tooling**
    - Export personal data (JSON) for current user.
    - Delete/anonymize current user (with confirmations).

### P2 — Hardening / operational

5. **Retention policy**
    - Define and implement retention for logs, uploads, and derived data (e.g., cached songs).
6. **Documentation**
    - Data Processing Register and vendor DPAs for any third-party processors (email provider, hosting, Spotify).

---

## 7) Concrete engineering changes suggested (implementation notes)

### 7.1 Storage: user media private

Options:

- **Private bucket + signed URLs** for media.
- **Authenticated media proxy endpoint** (Django view checks user permissions and streams file).

Also ensure:

- predictable paths aren’t guessable OR access is always checked.
- removing a user deletes their images.

### 7.2 PWA caching: exclude `/api/player/` responses

Update `apps/node_projects/frontend/korfbal-web/vite.config.ts` runtimeCaching:

- ✅ Implemented:
    - `/api/club/` + `/api/team/` remain cached.
    - `/api/player/` is `NetworkOnly`.

### 7.5 Privacy controls (implemented)

Backend + frontend now support user-controlled visibility:

- Profile picture visibility: `public` or `club`
- Stats visibility: `public` or `club` ("private" is deprecated and treated as `club`)

Enforcement is applied on key endpoints, and the UI shows a “Privé account” state when access is blocked.

### 7.6 Additional PII leak fixes (implemented)

Several legacy endpoints outside the main player API were updated to respect profile picture visibility and avoid leaking full names by default (e.g. legacy match tooling endpoints and team roster output).

### 7.3 DSAR endpoints (backend)

Minimal pattern:

- `GET /api/privacy/export/` → returns user + player + related objects.
- `POST /api/privacy/delete/` → deletes/anonymizes user and related models.

### 7.4 UI

- Link to:
    - Privacy policy
    - Cookie policy
    - Contact details (controller) and DSAR request method

---

## 8) Verification checklist

After changes:

- Confirm profile picture URLs are not publicly accessible without auth.
- Confirm SW cache does not store responses that include `email`/user identifiers.
- Confirm logout clears server session and client cache is cleared (if needed).
- Confirm policies are accessible in-app.

---

## Appendix: Key code references

Backend:

- `apps/django_projects/korfbal/korfbal/settings.py`
- `apps/django_projects/korfbal/apps/player/models/player.py`
- `apps/django_projects/korfbal/apps/player/api/serializers.py`
- `apps/django_projects/korfbal/apps/player/api/views.py`

Frontend:

- `apps/node_projects/frontend/korfbal-web/src/api/client.ts`
- `apps/node_projects/frontend/korfbal-web/src/pages/Profile/ProfileSettings.tsx`
- `apps/node_projects/frontend/korfbal-web/vite.config.ts`
