# ADR 0012 — The frontend is where the qualifications get dropped

**Status:** Accepted
**Date:** 2026-07-31
**Extends:** [ADR 0004](./0004-analytics-honesty.md), [ADR 0007](./0007-coach-placeholders.md), [ADR 0010](./0010-periodic-reports.md)

## Context

Milestone 5 builds the frontend: Next.js, TypeScript, Tailwind, TanStack Query, and the
three screens a trader actually lives in.

Eleven backend milestones exist to attach qualifications to numbers. Every payload carries
a sample size. Every finding carries an adjusted p-value and an `is_actionable` verdict.
Every model carries `is_deployable` and, when false, a plain-language `refusal`. Every
period-over-period metric carries `is_established` and a `direction` that distinguishes
*flat* from *unchanged*. Statistics that cannot be computed come back `null` with an
`undefined_reason` rather than as zero.

**All of that survives right up to the render function.**

The failure mode is not a bug anybody files. A designer asks for a trend arrow. The payload
has `difference` sitting right there next to `is_established`, and one of them is easier to
render. The dashboard ships with a green arrow on a four-point win-rate move across
twenty trades — exactly the noise ADR 0010 refuses to call a change — and the page looks
finished. No test fails. Nothing looks wrong. Twelve milestones of statistical discipline
are gone in one component, and the only symptom is that the product now looks *more*
confident than the one that was careful.

The same shape recurs everywhere: an `undefined_reason` becomes a `—`, a model refusal
becomes an empty state, a `Decimal` string becomes `parseFloat`.

## Decision 1 — components take evidence objects, never primitives

`Metric` takes an `Estimate`, not a `number`. `Change` takes a `MetricChange`. `Finding`
takes a `PatternFinding`. `ModelCard` takes a `ModelReport`.

This is the frontend's version of the placeholder contract in ADR 0007, and it works the
same way. The coach cannot write a number because it only ever writes `{{metric.key}}` and
Python substitutes; a component here cannot render a bare figure because the qualification
is in the same object as the value, so it is always in scope at the call site.

Passing `estimate.value` to `<Metric>` is a type error. That is the entire mechanism: the
lossy version has to be *worked at* rather than being the path of least resistance.

Three rules follow from the same principle:

* **An undefined value renders its reason, never a zero or a dash.** "not available" plus
  "no losing trades in this sample, so it is undefined" is information. `0` is a lie and
  `—` is a shrug.
* **A refusal is content, not an empty state.** When a model will not be served, the reason
  is the most useful thing on the screen — "resampling your sessions puts the true figure
  anywhere from −0.058 to 0.207" tells a trader what to do; a grey "no data yet" card tells
  them nothing and implies the platform is incomplete rather than careful.
* **A cost is attached only to an established leak.** An unestablished pattern is shown —
  the backend stores every test it ran, and hiding failures would make recurrence
  unfalsifiable — but labelled *examined*, never *found*, and never beside a dollar figure.

## Decision 2 — `unchanged` is visually distinct from `flat`

`MetricChange.direction` has four values and two of them look identical to a careless
implementation. `flat` means the measured values matched. `unchanged` means *the
measurement could not tell them apart.*

Collapsing them into one grey dash reports a settled fact where there is an open question,
and it is the single most likely rendering mistake on the reports screen, because on a
typical monthly report almost every metric comes back `unchanged`. A UI that makes that
look like "nothing moved" turns the honest result into a boring one.

So `unchanged` renders with a distinct glyph, distinct styling, the backend's own sentence
about chance, and a screen-reader-only clarification. There is a test asserting the two
render differently, because the failure is a CSS class away at all times.

## Decision 3 — decimals stay strings to the point of render

Money and probabilities arrive as strings, because they are `Decimal` in Python precisely
so they do not drift. `JSON.parse` with a reviver that coerced them to numbers would undo
that in one line, at the last possible moment, for convenience.

`Intl.NumberFormat` needs a number, so the conversion happens — but only inside
`@/lib/format`, at the point of display, and never on a value that is subsequently
compared, summed or stored. Nothing in the app does arithmetic on a figure it is about to
show.

## Decision 4 — dark by default, dense, monospaced numerics

Not a theme toggle. This is a tool people sit in front of for hours, often before dawn,
beside a broker platform that is already dark; a light default is the first thing every
user would change.

Numbers are monospaced and tabular everywhere. A column of P&L in a proportional font
cannot be scanned because the digits do not align, and scanning the blotter is the most
common action in the product. Navigation is a left rail rather than a top bar because
vertical space is the scarce resource on a dense table and horizontal space is comparatively
free on the monitors this runs on.

The dashboard leads with statistics rather than a large equity curve. The curve is the
conventional hero and the least informative thing available: it goes up or down and the
trader already knows which. Expectancy with its interval answers the question they actually
have.

## Consequences

* **The product will look less confident than its competitors**, and that is the intended
  outcome. Most months establish no changes; most traders' models are refused. A journal
  that renders arrows and probabilities on the same data is not more capable, it is less
  careful — but it will demo better, and that pressure is worth naming in writing.
* Query defaults are conservative: 60-second stale time, no refetch on window focus, and
  **429 is never retried**. The expensive endpoints are rate limited, and a focus-refetch
  storm would spend a trader's allowance on screens they are not looking at. Retrying into
  a rate limit turns a small overage into a sustained one.
* Job state is the one thing polled, at five seconds, because it changes without the user
  acting and the endpoint is cheap.
* The trade blotter renders no R multiple for a trade with no recorded stop. Filling that
  gap would be the display-layer version of imputing a missing value, which the ML layer
  refuses to do upstream.
* The evidence components are the natural place for the next milestone's replay UI to hang
  its markers, and the contract extends to it: a replay that draws a stop line for a trade
  with no recorded stop would be inventing one.
* Clerk is wired at the layout boundary but the API client takes a token parameter rather
  than reaching for a global, so the client stays testable without an auth provider mounted.
