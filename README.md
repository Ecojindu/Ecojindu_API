# Ecojindu Shuttle — Integrations & Agent Gateway

Everything Ecojindu Shuttle exposes to the outside world:

* **WhatsApp booking bot** — a stateful, rule-based Twilio flow that takes a passenger
  from "hi" to a paid QR ticket without leaving the chat.
* **AI agent gateway** — `/agent/v1`, an API-key-protected REST surface built for the
  Google ADK booking agent. Start at **[`docs/AGENT_INTEGRATION.md`](docs/AGENT_INTEGRATION.md)**.
* **Webhooks** — Paystack (signature-verified, idempotent) and Twilio status callbacks.

This service holds **no business logic of its own**. It authenticates callers, shapes
requests, and delegates to `ecojindu-backend` over an internal service key. That keeps
one source of truth for fares, seats and credits.

```
ecojindu-backend                    FastAPI · owns Postgres · :8000
ecojindu-api       ← you are here   FastAPI · WhatsApp + agent gateway · :8001
ecojindu-web                        Next.js · customer site · :3000
ecojindu-admin                      Next.js · operations + driver portal · :3001
```

---

## Setup

**Requirements:** Python 3.11+, and `ecojindu-backend` running (it owns the database).

```bash
cd ~/Desktop/ecojindu-api

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# SERVICE_API_KEY must match the backend's SERVICE_API_KEY exactly.

uvicorn app.main:app --reload --port 8001
```

Open **http://localhost:8001/docs**.

```bash
curl -s localhost:8001/health
# {"status":"ok","database":"ok","backend":"ok","twilio":"mock","environment":"development"}
```

`database: ok` means it can reach `whatsapp_sessions`; `backend: ok` means the core
service is answering.

---

## Runs with zero credentials

| Var | Default | Effect |
|---|---|---|
| `TWILIO_MOCK` | `true` | Outbound WhatsApp messages are logged verbatim instead of sent |
| `PAYSTACK_MOCK` | `true` | Webhook signature verification is skipped locally |
| `TWILIO_VALIDATE_SIGNATURE` | `false` | Inbound Twilio signatures aren't enforced locally |

**Turn all three off in production.**

You can drive the entire WhatsApp conversation locally by POSTing the same form body
Twilio would, and reading the replies out of the log:

```bash
curl -s -X POST localhost:8001/webhooks/twilio/whatsapp \
  -d "From=whatsapp:+2348077778888" -d "Body=hello"
```

---

## Environment

| Var | Notes |
|---|---|
| `BACKEND_BASE_URL` | Where `ecojindu-backend` lives |
| `SERVICE_API_KEY` | **Must match the backend's value.** Sent as `X-Service-Key` |
| `DATABASE_URL` | The same `ecojindu` database. This service only touches `whatsapp_sessions` |
| `AGENT_API_KEYS` | Comma-separated. Issue one per consumer so they rotate independently |
| `AGENT_RATE_LIMIT_PER_MINUTE` | Per-key throttle, default 120 |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | From the Twilio console |
| `TWILIO_WHATSAPP_FROM` | `whatsapp:+14155238886` is the sandbox; swap for your approved sender |
| `PAYSTACK_SECRET_KEY` | Only used to verify webhook signatures |
| `WHATSAPP_SESSION_TIMEOUT_MINUTES` | Idle conversations reset to the greeting (default 30) |

See `.env.example` for the annotated full list.

---

## The WhatsApp flow

```
idle ──MENU──▶ choosing_route ──▶ choosing_date ──▶ choosing_trip
     ──▶ choosing_seats ──▶ collecting_name ──▶ collecting_email
     ──▶ awaiting_payment ──▶ (Paystack settles) ──▶ QR ticket delivered
```

State lives in `whatsapp_sessions.context` (JSONB), keyed by phone number.

**Global commands, valid in any state:**

| Command | Does |
|---|---|
| `MENU` | Start a fresh booking (also `hi`, `hello`, `start`, `book`) |
| `MY BOOKING EJS-8K3F2` | Status lookup |
| `CANCEL` | Cancel the in-progress booking, with a yes/no confirmation |
| `HELP` | Command reference |

**It's forgiving on purpose.** `tmrw`, `2moro`, `tomorow`, `friday`, `14/08/2026` all
parse as dates; `two`, `a couple`, `3 seats` all parse as counts; menu picks accept
`2`, `2.`, `option 2`, `#2`. Anything unrecognised re-prompts with the options rather
than dead-ending. Sessions idle for 30 minutes reset to the greeting.

**Subscribers skip payment.** Before quoting, the flow checks the sender's number for
ride credits — if there are enough, the booking confirms instantly and the QR arrives
in the chat with no payment step at all.

### After payment

Paystack fires `charge.success` → this service verifies the signature → relays the raw
body to the backend (which confirms the booking, issues the QR, sends email + SMS) →
then pushes the QR image into the WhatsApp thread as a media message.

Guarded by a `ticket_sent` flag in the session, so Paystack's retries don't re-send it.
If Twilio was down at settlement time:

```bash
curl -X POST localhost:8001/webhooks/paystack/replay/EJSBK-XXXX
```

---

## The agent surface

`/agent/v1`, authenticated with `X-API-Key`. Nine endpoints, one per caller intent:

```
GET  /agent/v1/routes                        routes + fares + pickup points
GET  /agent/v1/availability?route_id&date    live seat availability
POST /agent/v1/quote                         fare quote
POST /agent/v1/bookings                      book + Paystack link
GET  /agent/v1/bookings/{ref}                status
POST /agent/v1/bookings/{ref}/cancel
GET  /agent/v1/subscriptions/{phone}         credit balance
POST /agent/v1/subscriptions/{phone}/book    credit booking, no payment
POST /agent/v1/tickets/{ref}/resend
GET  /agent/v1/health
```

Every response carries a `message` or `summary` written to be **said verbatim**, so the
agent never has to compose prose from raw fields. Errors use the same envelope with a
passenger-safe `message`.

**→ [`docs/AGENT_INTEGRATION.md`](docs/AGENT_INTEGRATION.md)** has request/response
examples for every endpoint, four worked call flows, suggested ADK tool definitions
and prompt guidance.

---

## Webhook endpoints

| Endpoint | Auth |
|---|---|
| `POST /webhooks/twilio/whatsapp` | `X-Twilio-Signature` when `TWILIO_VALIDATE_SIGNATURE=true` |
| `POST /webhooks/twilio/status` | Delivery-status callbacks, logged |
| `POST /webhooks/paystack` | `x-paystack-signature`, HMAC-SHA512 of the raw body |

Point Paystack at **either** this service or the backend — processing converges either
way, because the backend keys every delivery in its `webhook_events` ledger.

---

## Tests

```bash
pytest
```

| File | Covers |
|---|---|
| `tests/test_flow_parsing.py` | Date, count, menu-choice, name, email and reference parsing, including the typos passengers actually send |
| `tests/test_agent_auth.py` | Every agent endpoint rejects missing and wrong keys; rate limiting; Paystack signature verification; OpenAPI completeness |

The auth suite runs without the backend up — it asserts on 401s, which happen before
any upstream call.

---

## Deploying to Cloud Run

```bash
PROJECT=your-gcp-project
REGION=africa-south1

gcloud builds submit --tag gcr.io/$PROJECT/ecojindu-api

gcloud run deploy ecojindu-api \
  --image gcr.io/$PROJECT/ecojindu-api \
  --region $REGION --platform managed --allow-unauthenticated \
  --add-cloudsql-instances $PROJECT:$REGION:ecojindu-pg \
  --set-env-vars "ENVIRONMENT=production,DEBUG=false,LOG_JSON=true,TWILIO_MOCK=false,PAYSTACK_MOCK=false,TWILIO_VALIDATE_SIGNATURE=true,BACKEND_BASE_URL=https://ecojindu-backend-xxxx.run.app" \
  --set-secrets "DATABASE_URL=ecojindu-db-url:latest,SERVICE_API_KEY=ecojindu-service-key:latest,AGENT_API_KEYS=ecojindu-agent-keys:latest,TWILIO_AUTH_TOKEN=twilio-token:latest,PAYSTACK_SECRET_KEY=paystack-secret:latest" \
  --min-instances 1 --max-instances 10 --cpu 1 --memory 512Mi
```

After deploying:

1. **`PUBLIC_BASE_URL`** must be the deployed URL — Twilio signature validation hashes
   the request URL, so a mismatch rejects every inbound message.
2. Point the **Twilio WhatsApp sender's** "When a message comes in" webhook at
   `https://<api-url>/webhooks/twilio/whatsapp` (HTTP POST), and its status callback at
   `/webhooks/twilio/status`.
3. Point the **Paystack** dashboard webhook at `https://<api-url>/webhooks/paystack`
   (or at the backend's — either works).
4. Issue the ADK teammate their own key by appending to `AGENT_API_KEYS`.
5. Keep `--min-instances 1` so the first WhatsApp message of the morning isn't a cold start.

---

## Project layout

```
app/
  core/          config, logging, errors, API-key auth + signature verification
  services/
    backend.py   typed HTTP client for ecojindu-backend
    twilio.py    WhatsApp sender (mockable)
    sessions.py  conversation-state store over whatsapp_sessions
  whatsapp/
    flow.py      the conversation state machine and its parsers
    router.py    Twilio inbound webhook
  agent/
    schemas.py   documented request/response contracts
    router.py    /agent/v1
  webhooks/      Paystack receiver + WhatsApp ticket delivery
docs/AGENT_INTEGRATION.md   ← give this to the ADK teammate
tests/
```

---

Ecojindu Shuttle · Nnenna Otti Bus Terminal, Umuahia, Abia State
`jinduinc@gmail.com` · +234 815 447 1570 · @ecojindu.ng
*Bridging Cities, Powering Green Mobility.*
