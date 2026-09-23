# Vetting Report Format — the House Convention for Presenting a Scan

Article Layer: 2
Article Role: capability_reference
Article Tags: planning-stage:execution, evidence-category:capability-reference, domain:code-vetting, consumer_profile:both

Embedding Description: The house convention for how the AI code-vetting suite presents a scan result — the elevated report format that assembles into a Google Doc using headings, tables, bold, and text and background color only, never images or scripts. Covers the section architecture (title block; executive summary with an at-a-glance risk-posture banner, a severity scorecard split into confirmed-versus-unverified, and an optional per-dimension posture strip; scope and methodology and provenance; the coverage-honesty matrix; structural quality metrics; findings-by-severity as consistent cards; strengths and positive controls; a remediation roadmap; an appendix), the exact per-finding card template (id, severity with CWE and CVSS where apt, dimension, confidence, location with source-to-sink data-flow, evidence, impact, remediation with a corrected snippet, references, provenance), the Docs-workable severity color palette paired with an emoji so color is never the only signal, the calibrate-not-verdict framing for a foreign repo, and the adopt-reject ledger against Snyk, SonarQube, CodeQL, Semgrep, and professional penetration-test reports.

**When you need this**: assembling a vetting scan into a shareable report or a Google Doc; deciding what sections a suite report carries and in what order; writing one finding so it reads the same as every other finding; choosing severity colors that survive a colorblind reader or a black-and-white print; framing a report on a repo the operator does not own; explaining why the report reports what it could NOT inspect, not only what it found.

The full specification — the section architecture, the verbatim card template, the native-Docs hex palette, and the adopt/reject ledger — is `workbench/archive/2026-07-21_vetting_report_format_spec.md`. This article is the canonical house-convention capture of it. Four principles distinguish this format from a tool dashboard.

## Dual audience, one document

The report serves an owner and an engineer at once. The executive summary is plain business language, so an owner skims it in about a minute and knows the risk posture. Each finding card then carries full evidence and a concrete fix, so an engineer acts without a second document. This is penetration-test-report discipline: the executive summary avoids jargon, the technical findings carry reproduction and remediation.

## Coverage honesty is the differentiator

Every leading scanner reports what it found. Almost none report what it could NOT look at. The suite's coverage-honesty matrix — each scanner marked examined, not-applicable, or tool-absent — is the trust anchor. A clean report next to an explicit account of exactly what was and was not inspected is more credible than a clean report alone. A tool-absent row is a recorded gap surfaced, never a silent pass.

## Calibrate, not verdict, for a foreign repo

A report on a repository the operator does not own carries a risk POSTURE plus confidence, never a pass/fail judgment on someone else's engineering. The report says so explicitly and early. A self-vet may carry a harder verdict because the commit gates own that surface; a foreign-vet does not. This is why the format rejects SonarQube-style A-to-E letter grades on external code and uses a Clean, Gaps, Weakness, or Critical posture strip instead.

## Consistent cards and visible confidence

Every finding is the same card shape regardless of which scanner or critic produced it, so a reader learns the card once. Every card quotes the offending code, the data-flow path, or the scanner output — evidence, never a naked assertion. The confidence field separates a confirmed finding from a candidate, which makes the verification layer visible and tells the reader what the suite proved versus what a tool merely flagged.
