# ADR-22: Review dashboard

Status: proposed
Source: S3-04

## Context
Reviewers need to see a draft, why it exists and why the Guard said what it said, and then approve or reject it. The review
API (ADR-18) exists; what was missing was a face for it. It carries message bodies, so it handles personal data.

## Decision
- **A thin Streamlit app over the review API, nothing else.** It holds no database access and no secrets of its own. It signs in
  with the same bearer token the API accepts, typed into a password field and kept only in the browser session. It is never
  written to disk or logged, never shown in an error, and the sign-in form is not drawn once signed in.
- **Logic outside the screen.** `ui/review_client.py` (HTTP), `ui/view_model.py` (what the reviewer reads) and `ui/dashboard.py`
  (selection, approve, reject, failures) are plain Python with tests. `ui/app.py` only draws them.
- **A remote service must use https.** Plain http is accepted for localhost only, so a token never travels in the clear.
  Redirects are not followed, so a token cannot be forwarded to another host.
- **Everything from a draft is shown as plain text.** The subject and body go through `st.text`, never markdown or HTML, so nothing a
  model wrote is interpreted as markup. A test proves it with hostile input.
- **Approve only when the checks passed.** The screen disables Approve otherwise and says why; the server refuses it anyway. In shadow
  mode (tier 0) the screen says that approving sends nothing.
- **Rejecting needs a reason from a fixed list.** The codes feed calibration (ADR-18); there is no free text, so no personal data.
- **The detail carries the lead's scores and the latest Guard findings.** `ReviewQueue.get` returns them as ids, scores, codes and
  timestamps, never a contact. Every Guard rule code has a plain-language label, and a test fails if a new code has none.
- **No accounts of its own.** Identity is the token. Bind the app to localhost behind a TLS proxy (cutover C14).

## Consequences
- Editing a draft is in the API but not on the screen yet.
- A single static token per reviewer is the only identity until SSO replaces it.
- The Streamlit tests need `streamlit` installed in CI; they import it at the top so a missing install fails loudly.

## Revisit when
SSO arrives, editing is added to the screen, or more than one reviewer works at once and needs to see who decided what.
