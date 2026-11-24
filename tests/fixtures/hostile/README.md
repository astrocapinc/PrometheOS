# Hostile synthetic fixtures (A02, CP10.2; decision 0016)

Invented test inputs for the untrusted-content boundary (R25, C10-AUTH). Each one carries text
that tries to give instructions: approve requests, call tools, mark claims supported, widen
egress, change the provider route, send data out, print a secret. They are data. The tests in
`tests/test_cp10_untrusted.py` import, render and process them through the service, MCP and
HTTP, and prove that nothing beyond their role as data changes.

- `paper.pdf`: a one-page PDF with the injection as page text (uncompressed content stream).
- `abstract.bib`: a bibliography entry whose abstract carries the injection.
- `page.html`: a saved web page with the injection in a hidden element, a meta tag, a comment and
  a script.
- `transcript.txt`: a meeting transcript in which a speaker voices the injection.
- `model_output.json`: a model reply in the meeting-extract shape whose item text is the
  injection (`__EVIDENCE__` is replaced by a real evidence reference in the test).
- `model_output_smuggling.json`: the same reply with extra fields that try to add operations,
  approvals and `confirmed: true`.

No real person, project or service is named; `exfil.example.test` is a reserved test domain.
Regenerated only by hand; the bytes are part of the reviewed test inputs.
