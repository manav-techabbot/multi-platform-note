# Multi Platform Note Posting

A small FastAPI service that takes a short note and posts it to a bunch of platforms at once.

Platforms added so far:

- **Dev.to (DEV Community)** - tech blogs
- **GitHub Gist** - used for sharing code snippets
- **Bluesky** - decentralized text based social media
- **Tumblr** - social media / blogging
- **JSONPlaceholder** - fake CRUD API, only used to simulate a post

```
POST http://localhost:4000/posting
{"message": "Check out our latest business platform. https://example.com"}
```

The client only sends the message. Which platforms get used is decided on the server side from `.env`. All platforms are called at the same time and independently, and you get back one result per platform.

No LLMs, scraping or browser automation here, it just calls each platform's official API.

## What it does

- Single endpoint `POST /posting` with validation (message is required, not blank, max 200 chars, and must have exactly one `http`/`https` URL).
- Each platform has its own publisher class behind a common interface. To add a new platform you write one publisher and register it in the registry.
- Platforms run in parallel. If one fails, times out or throws, the others still go through.
- Message is formatted per platform (escaped Markdown with the link for DEV/Gist, link facets for Bluesky, NPF link formatting for Tumblr). It does not add hashtags or any extra text.
- Retries only happen when it's safe to retry (no chance of a duplicate post). Uses exponential backoff with jitter and respects `Retry-After`.
- Tumblr uses OAuth 2.0, so there's an authorize flow built in (`GET /tumblr/authorize` and `GET /tumblr/callback`) with a `state` check. Refreshed tokens are saved to a git-ignored file.
- Errors are cleaned up and grouped into categories. Keys/tokens never show up in responses or logs.
- Post URLs only come from the actual API responses. JSONPlaceholder is fake so it's never counted as a real post.

## Platforms

| Platform | API | Auth | Result |
| --- | --- | --- | --- |
| DEV Community | `POST https://dev.to/api/articles` (Forem API v1) | `api-key` header | Real article |
| Bluesky | AT Protocol `com.atproto.repo.createRecord` via the `atproto` SDK | App Password | Real post |
| Tumblr | `POST https://api.tumblr.com/v2/blog/{blog}/posts` (NPF) | OAuth 2.0 Bearer | Real post |
| GitHub Gist | `POST https://api.github.com/gists` | Personal access token (`gist`) | Real gist |
| JSONPlaceholder | `POST https://jsonplaceholder.typicode.com/posts` | None | **Simulated**, nothing is saved |

Setup for each one is further down in [Setting up each platform](#setting-up-each-platform).

## Tech stack

Python 3.11+ with uv, FastAPI, Uvicorn, Pydantic v2, pydantic-settings, HTTPX (one shared `AsyncClient`), the `atproto` SDK for Bluesky, and pytest + pytest-asyncio + respx for tests. No database or queue.

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/). It installs the right Python version (3.12, pinned in `.python-version`) and the dependencies, so you don't need to set up a virtualenv yourself.
- Accounts/keys for whichever platforms you want to post to. JSONPlaceholder doesn't need anything.

## Installation

```bash
git clone <repo-url>
cd multi-platform-note
uv sync
```

`uv sync` creates `.venv` and installs everything from `uv.lock` (including the test dependencies). All the commands below use `uv run` so you don't have to activate anything.

## Environment setup

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

Then fill in the keys for the platforms you want to use. The setup section below explains where to get each one.

| Variable | What it's for | Default |
| --- | --- | --- |
| `ENABLED_PLATFORMS` | Comma separated list of platforms to post to | all five |
| `REQUEST_TIMEOUT` | Read/write timeout in seconds | `15` |
| `CONNECT_TIMEOUT` | Connect timeout in seconds | `5` |
| `MAX_RETRIES` | How many times to retry after the first try (only for safe errors) | `2` |
| `PLATFORM_MAX_RETRIES` | Per platform overrides, e.g. `devto=3,tumblr=0` | empty |
| `LOG_LEVEL` | Python log level | `INFO` |
| `DEVTO_BASE_URL`, `BLUESKY_SERVICE_URL`, `BLUESKY_WEB_URL`, `TUMBLR_API_BASE_URL`, `TUMBLR_AUTHORIZE_URL`, `TUMBLR_TOKEN_URL`, `GITHUB_API_BASE_URL`, `JSONPLACEHOLDER_BASE_URL` | Platform API URLs. These aren't hardcoded in the code so they have to be set. `.env.example` already has the right values. | none |
| `DEVTO_API_KEY` | DEV Community API key | |
| `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD` | Bluesky handle and App Password | |
| `TUMBLR_BLOG_IDENTIFIER`, `TUMBLR_CONSUMER_KEY`, `TUMBLR_SECRET_KEY`, `TUMBLR_REDIRECT_URI`, `TUMBLR_ACCESS_TOKEN`, `TUMBLR_REFRESH_TOKEN` | Tumblr blog name and OAuth 2.0 stuff. Tokens can be left empty if you use `/tumblr/authorize`. | none |
| `TUMBLR_TOKEN_STORE_PATH` | Where the Tumblr tokens get saved | `.secrets/tumblr_tokens.json` |
| `GITHUB_API_VERSION`, `GITHUB_GIST_TOKEN`, `GITHUB_GIST_PUBLIC` | GitHub API version, access token, and whether gists are public | public |
| `JSONPLACEHOLDER_USER_ID` | `userId` sent to JSONPlaceholder | `1` |

All the `*_URL` / `*_URI` values have to start with `http://` or `https://`, otherwise the app won't start and will tell you which one is wrong.

If a platform is enabled but its keys are missing, the app still runs. That platform just comes back as `failed` with a `configuration` error that lists the missing variables. If you don't want a platform at all, remove it from `ENABLED_PLATFORMS`.

`.env` is in `.gitignore`, don't commit it.

## Running the app

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 4000
```

Swagger UI is at <http://localhost:4000/docs>.

## Setting up each platform

Every platform needs a one time setup. After that, testing it is always the same:

1. Restart the server after changing `.env` (settings are only read on startup).
2. Send a message with the `curl` command from [Calling the API](#calling-the-api).

If you want to test just one platform, put only that one in `.env`, e.g. `ENABLED_PLATFORMS=gist`.

### Tumblr

Tumblr took the most work since it uses OAuth instead of a simple API key.

1. Make a Tumblr account at <https://www.tumblr.com>. Your blog name (the `yourblog` part of `yourblog.tumblr.com`) goes in `TUMBLR_BLOG_IDENTIFIER`.
2. Register an app at <https://www.tumblr.com/oauth/apps> > **Register application** and fill in:

   | Field | Value |
   | --- | --- |
   | Application Website | `http://localhost:4000` |
   | Default callback URL | `http://localhost:4000/tumblr/callback` |
   | OAuth2 redirect URLs | `http://localhost:4000/tumblr/callback` |

   The redirect URL has to match `TUMBLR_REDIRECT_URI` in `.env` exactly, otherwise Tumblr gives a `redirect_uri_mismatch` error after you click **Allow**.
3. On the app page, copy the **OAuth Consumer Key** and **Secret Key** (click "Show secret key") into `.env`:

   ```env
   TUMBLR_BLOG_IDENTIFIER=yourblog
   TUMBLR_CONSUMER_KEY=<OAuth Consumer Key>
   TUMBLR_SECRET_KEY=<Secret Key>
   TUMBLR_REDIRECT_URI=http://localhost:4000/tumblr/callback
   ```

   Keep `TUMBLR_ACCESS_TOKEN` and `TUMBLR_REFRESH_TOKEN` empty, the next step fills them in.
4. Start the server and open <http://localhost:4000/tumblr/authorize> in the browser, then click **Allow**. Tumblr redirects back to `/tumblr/callback`, which shows `"status": "authorized"` and saves the tokens to `.secrets/tumblr_tokens.json`. You only need to do this once, the access token gets refreshed automatically after that.
5. Send a test message.

If it fails, the usual cause is the redirect URL not matching, or the blog name being wrong (use just the name, not the full URL).

### Bluesky

1. Make an account at <https://bsky.app>.
2. Create an **App Password** at <https://bsky.app/settings/app-passwords> (**Settings > Privacy and Security > App Passwords > Add App Password**). Use this and not your real password, it can be revoked on its own and can't change account settings.
3. Add your handle (without the `@`) and the App Password to `.env`:

   ```env
   BLUESKY_HANDLE=yourname.bsky.social
   BLUESKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
   ```

4. Send a test message.

### GitHub Gist

1. Make a GitHub account at <https://github.com/signup> if you don't have one.
2. Create a personal access token (see GitHub's guide [Managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)). Either type works:
   - **Fine-grained** (recommended): <https://github.com/settings/personal-access-tokens/new>, under **Account permissions** set **Gists** to **Read and write**.
   - **Classic**: <https://github.com/settings/tokens/new>, only tick the **`gist`** scope.
3. Add the token to `.env`:

   ```env
   GITHUB_GIST_TOKEN=github_pat_xxx
   GITHUB_GIST_PUBLIC=true
   ```

   Set `GITHUB_GIST_PUBLIC=false` if you want secret (unlisted) gists instead.
4. Send a test message.

### DEV Community

1. Make an account at <https://dev.to/enter>.
2. Generate an API key at <https://dev.to/settings/extensions>. Under **DEV Community API Keys**, add a description and click **Generate API Key**.
3. Add the key to `.env`:

   ```env
   DEVTO_API_KEY=your-api-key
   ```

4. Send a test message. Each message gets posted as a short DEV article.

### JSONPlaceholder

Nothing to set up, it's on by default and doesn't need an account or key.

Keep in mind JSONPlaceholder is a **fake** API. It accepts the post and replies like it was created (always `id: 101`), but nothing is actually saved. `GET /posts/101` returns 404 and there's no public URL. Because of that its result comes back with `delivery_mode: "simulated"`, `posted_url: null` and `public_access: false`, and it never makes `success` true by itself.

## Calling the API

```bash
curl -X POST "http://localhost:4000/posting" \
  -H "Content-Type: application/json" \
  -d "{\"message\":\"Check out our latest business platform. https://example.com\"}"
```

Note: if you have real keys set up, this will create real public posts.

### Validation

The body has to be a JSON object with only a `message` field. You get a 422 if the message:

- is missing, empty or just spaces
- is over 200 characters
- has no URL, or more than one URL
- has a URL that's malformed, has a bad hostname, has no scheme (`www.example.com`), or isn't `http`/`https`

You also get a 422 if the body has any field other than `message`.

A full stop at the end of a sentence isn't counted as part of the URL, so in `Visit https://example.com.` the URL is `https://example.com`.

### Response

```json
{
  "success": true,
  "partial_success": true,
  "simulated_count": 1,
  "results": [
    {
      "platform": "GitHub Gist",
      "status": "success",
      "delivery_mode": "published",
      "post_id": "aa5a315d61ae9438b18d",
      "posted_url": "https://gist.github.com/aa5a315d61ae9438b18d",
      "public_access": true,
      "date": "2026-11-17",
      "time": "10:30:00",
      "error": null,
      "details": null
    },
    {
      "platform": "DEV Community",
      "status": "failed",
      "delivery_mode": "published",
      "post_id": null,
      "posted_url": null,
      "public_access": null,
      "date": null,
      "time": null,
      "error": {
        "category": "authentication",
        "message": "DEV Community returned HTTP 401: unauthorized",
        "http_status": 401
      },
      "details": null
    },
    {
      "platform": "JSONPlaceholder",
      "status": "success",
      "delivery_mode": "simulated",
      "post_id": null,
      "posted_url": null,
      "public_access": false,
      "date": null,
      "time": null,
      "error": null,
      "details": "Simulated POST accepted (placeholder id 101). The resource is not persisted or publicly accessible."
    }
  ]
}
```

- `success` is only true if at least one **real** platform posted the message. JSONPlaceholder alone doesn't count.
- `partial_success` is true if one real platform worked but something else failed, was simulated, or wasn't public.
- `date` and `time` are in UTC.
- A valid request always returns 200 with all the results, even if every platform failed.

#### Error categories

When a platform fails, `error.category` tells you roughly why:

| Category | Meaning |
| --- | --- |
| `configuration` | Keys/settings for that platform are missing |
| `authentication` | Bad or expired key/token (401) |
| `permission_denied` | Token doesn't have the right permission (403) |
| `invalid_request` | Platform said the request was bad (400) |
| `not_found` | Blog/resource doesn't exist (404) |
| `conflict` | Platform reported a conflict (409) |
| `content_rejected` | Platform didn't accept the content (422) |
| `rate_limited` | Got a 429 from the platform |
| `platform_unavailable` | 5xx / platform is down |
| `network` | Couldn't connect to the platform |
| `uncertain_delivery` | The post may have gone through but the response was lost, so it isn't retried |
| `unexpected_response` | The platform replied with something we couldn't read |

## Running tests

```bash
uv run pytest -v
```

There are 330 tests and all external APIs are mocked, so you don't need any keys or internet, and the tests can't create real posts.

## Project structure

```text
app/
  main.py                  app factory, lifespan, generic error handler
  config.py                settings loaded from env / .env
  schemas.py               request, result and response models
  exceptions.py            publishing errors and HTTP status mapping
  api/posting.py           POST /posting route
  api/tumblr_oauth.py      GET /tumblr/authorize and /tumblr/callback
  oauth/                   Tumblr OAuth client, token manager, token file, state store
  core/                    shared HTTP client, retry logic, log redaction
  publishers/              base class, registry and the five publishers
  services/posting_service.py   runs all publishers in parallel and combines results
  utils/                   URL validation, content formatting, timestamps
tests/                     validation, service, error handling, API and publisher tests
implementation-logs-screenshots/   my notes while building this + screenshots
```

## Known limitations

- I tested live posting manually on all five platforms with my own accounts. The automated tests only use mocks, so after adding your own keys it's worth sending one real message to check everything works.
- Exactly-once posting can't be guaranteed. If a platform accepts the post but the response gets lost, it's reported as `uncertain_delivery` and not retried. None of the four real platforms support an idempotency key.
- `/tumblr/authorize` and `/tumblr/callback` have no auth on them, so keep the service local or put it behind your own auth.
- The Bluesky SDK always tags posts with language `en`.
- `public_access` is based on what the API response says (post status, visibility). It can't tell if a whole blog is private.
- No queue or persistence. If the server restarts mid request that request is lost and nothing retries it later.
