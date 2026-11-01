# Review dashboard

A Streamlit face for the review API (S2-05): the drafts waiting for a decision, why each one is there, and Approve / Reject.

Run it from the repository root:

    pip install -r ui/requirements.txt
    REVIEW_API_URL=http://127.0.0.1:8080 streamlit run ui/app.py --server.address 127.0.0.1

- **Sign in** with a review token (the same bearer token the API accepts). It is typed into a password field and kept only in
  that browser session. Nothing is written to disk or logged.
- **Remote services must use https.** Plain http is accepted for localhost only, so a token is never sent in the clear.
  Redirects are not followed.
- **Approve** is enabled only when the checks passed. In shadow mode (tier 0) an approval records your decision and sends nothing.
- **Reject** needs a reason from a fixed list; the reasons feed back into calibration.
- The dashboard shows ids, scores, codes and the draft. It never shows an address, a name or a consent reference.
- Bind it to localhost behind a TLS proxy; it has no accounts of its own (cutover item C14).
