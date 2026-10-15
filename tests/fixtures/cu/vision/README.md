# Recorded vision fixtures (REAL responses — no mocks)

The `cases/*.json` files hold REAL recorded responses from the live Jev
(TypeSafe System One) API and the local CLIProxy OpenAI-compatible endpoint,
captured on 2026-10-15 by `scripts/record_cu_fixtures.py` (spend: cents —
about 56k input / 1.1k output tokens in total). Unit tests in
`tests/test_cu_vision.py` replay them through a fake transport; nothing here
is generated or hand-written.

Recorded cases (model, expected outcome):

| file | model | case | expected / result |
| --- | --- | --- | --- |
| `cliproxy_image_probe.json` | `gemini-3.8-flash-high` (CLIProxy) | tiny solid-red image, "mostly red?" | noul 1.0 — image input works on CLIProxy |
| `locate_submit_jev114.json` | `jev-1.14.0` | Box + presence Noul, Submit button on the form | found |
| `locate_email_jev114.json` | `jev-1.14.0` | Box + presence Noul, Email field | found |
| `locate_delete_account_jev114.json` | `jev-1.14.0` | Box for a non-existent "Delete account" button | NOT found (presence 0.01) |
| `verify_thanks_pass_jev114.json` | `jev-1.14.0` | "Thanks, Ada" page vs expected thanks message | pass (yes 0.98) |
| `verify_thanks_fail_jev114.json` | `jev-1.14.0` | pre-submit form vs the same expectation | fail (yes 0.00) |
| `safety_payment_jev114.json` | `jev-1.14.0` | payment dialog screen | pause (payment 1.0, dialog 0.99) |
| `safety_form_jev114.json` | `jev-1.14.0` | plain form screen | see jaggedness note below |
| `box_jev113_error.json` | `jev-1.13.0` | Box question | real `400 Invalid request` (Box is 1.14-only) |
| `locate_submit_openai_fallback.json` | `gemini-3.8-flash-high` (CLIProxy) | same locate, prompted JSON coordinates | found, grounding "fallback" |

## Known jaggedness (recorded as-is, no cherry-picking)

- `safety_form_jev114`: on the reworded risk prompt Jev now answers `none`
  (0.58) but still puts 0.41 on `send_or_publish` (the page has a Submit
  button). Because the safety gate pauses when ANY non-`none` probability
  crosses the low 0.2 threshold (bias toward asking), this screen produces a
  false pause. An earlier, blunter prompt ("what kind of action...") made
  Jev call the form `send_or_publish` outright (0.97). The threshold sweep
  belongs to the phase-2 eval (P6); unit tests use the recorded payment
  case for pause behavior and synthetic low probabilities for the no-pause
  path.
- Box coordinates wobble a few pixels between identical repeat calls
  (compare `locate_submit_jev114` vs `locate_email_jev114` confidence).

## Privacy

- No API keys or other secrets are stored here. Only request metadata
  (model, headers, question shapes, token usage) and model answers.
- The screenshots (`*.png`) are of tiny local demo pages built for this
  repo; they contain no personal data. `tiny.png` is a solid-color square.
- The `<ref>` strings in stored instructions are per-recording placeholders;
  the gate uses fresh uuid references per request at runtime.
