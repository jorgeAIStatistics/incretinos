/**
 * Plain-English explanations of the technical terms the interface uses. One file, so a term is explained the
 * same way everywhere. Use through <Term k="estimand">…</Term> (components/ui/term.tsx).
 *
 * Writing rules: say what the term means and what it does not let you conclude; one or two short sentences;
 * no formula unless the term is the formula. Definitions follow ICH E9(R1) for estimands and standard
 * biostatistical usage for the rest. They describe concepts only: no entry may state a result.
 */
export type GlossaryEntry = { term: string; definition: string; caution?: string };

export const GLOSSARY = {
  estimand: {
    term: "Estimand",
    definition:
      "The exact treatment effect a trial sets out to estimate: which participants, which outcome, how events such as stopping treatment are handled, and how the groups are summarised and compared.",
    caution: "Two numbers for the same arm can both be correct if they answer different estimands.",
  },
  "treatment-regimen-estimand": {
    term: "Treatment-regimen estimand",
    definition: "The effect of being assigned to a treatment, counting everyone whether or not they stayed on it (a treatment-policy strategy). It reflects how the drug was actually taken within the trial.",
  },
  "efficacy-estimand": {
    term: "Efficacy estimand",
    definition: "The effect the treatment would have had if every participant had stayed on it (a hypothetical strategy). It answers a “what if” question.",
    caution: "It is usually larger in size than the treatment-regimen estimand and must not be compared with one.",
  },
  "intercurrent-event": {
    term: "Intercurrent event",
    definition: "Something that happens after treatment starts and changes what the outcome means, or whether it can be measured: stopping the drug, starting rescue therapy, or death.",
  },
  "treatment-policy-strategy": {
    term: "Treatment-policy strategy",
    definition: "Outcomes are used as observed, whether or not the intercurrent event happened. Stopping treatment is treated as part of the treatment being studied.",
  },
  "hypothetical-strategy": {
    term: "Hypothetical strategy",
    definition: "The analysis estimates what the outcome would have been if the intercurrent event had not happened, for example if nobody had stopped treatment. This relies on statistical modelling of data that were not observed.",
  },
  "confidence-interval": {
    term: "Confidence interval",
    definition: "A range of values that are compatible with the trial’s data under the analysis used. A 95% interval comes from a method that would cover the true value in 95 of 100 repeated trials.",
    caution: "It is not a 95% chance that the true value lies in this particular range.",
  },
  "derived-interval": {
    term: "Derived interval",
    definition: "An interval calculated here from a reported estimate and its standard error, because the source gave no interval of its own. It is an inference and is always labelled as one.",
  },
  "standard-error": {
    term: "Standard error",
    definition: "How much an estimate would be expected to vary from one repetition of the trial to the next. Smaller means more precise.",
  },
  "non-inferiority-margin": {
    term: "Non-inferiority margin",
    definition: "The largest amount by which a new treatment may be worse than its comparator and still be accepted as not unacceptably worse. It is fixed before the trial starts.",
    caution: "Meeting the margin does not show that the new treatment is better.",
  },
  "multiplicity-control": {
    term: "Multiplicity control",
    definition: "A plan, fixed in advance, that limits the chance of a false-positive result when a trial tests several endpoints or doses.",
    caution: "A result outside that plan is exploratory, however small its p-value.",
  },
  disproportionality: {
    term: "Disproportionality",
    definition: "A screening measure for spontaneous reports: whether an event is reported more often with one drug than with other drugs in the same database.",
    caution: "It can flag something worth investigating. It cannot measure risk and it does not show that the drug caused the event.",
  },
  "spontaneous-report": {
    term: "Spontaneous report",
    definition: "An adverse-event report sent to a regulator by a clinician, a patient or the manufacturer, outside a clinical trial. Nobody knows how many people took the drug, so reports cannot be turned into a rate.",
  },
  "evidence-tier": {
    term: "Evidence tier",
    definition: "The kind of source a number came from: FDA labeling, an FDA review, a peer-reviewed article, registry-posted results, a press release and so on. The tier tells you how much independent scrutiny the source has had.",
  },
  "machine-extracted": {
    term: "Machine-extracted",
    definition: "Read by code from a structured registry record whose snapshot hash is stored. No person has checked it.",
  },
  "quote-anchored": {
    term: "Quote-anchored",
    definition: "The number and its supporting quote were found word for word in the source text.",
    caution: "This shows the number is in the source. It does not show that it was attached to the right study, arm, endpoint or estimand.",
  },
  "human-verified": {
    term: "Human-verified",
    definition: "An authorized reviewer approved this exact version of the record, and the decision is in the audit log. This is the only state that means a person has checked the value.",
  },
  topline: {
    term: "Topline result",
    definition: "The headline result a sponsor announces, usually by press release, before the full analysis is published or peer reviewed.",
  },
  "registry-posted-results": {
    term: "Registry-posted results",
    definition: "Results the sponsor entered on ClinicalTrials.gov. The registry checks them for quality and consistency; they are not peer reviewed.",
  },
  "primary-completion-date": {
    term: "Primary completion date",
    definition: "The date the last participant was examined for the primary outcome. An “estimated” date is the sponsor’s forecast and can move.",
  },
  "surrogate-endpoint": {
    term: "Surrogate endpoint",
    definition: "A measurement, such as body weight or HbA1c, used in place of an outcome people feel or that changes survival. A change in a surrogate does not by itself show a change in clinical events.",
  },
  "randomized-withdrawal": {
    term: "Randomized withdrawal",
    definition: "A design in which everyone first takes the drug, and those who continue are then randomized to keep taking it or switch to placebo. Change is measured from that randomization, not from study entry.",
  },
  "cross-study-comparison": {
    term: "Cross-study comparison",
    definition: "Setting results from two different trials side by side. Because participants were not randomized between the trials, differences in population, duration or analysis can explain the gap.",
    caution: "It is descriptive only and is not head-to-head evidence.",
  },
  "anchored-indirect-comparison": {
    term: "Anchored indirect comparison",
    definition:
      "An estimate of drug A versus drug B built from two trials that share a comparator C: the A-versus-C effect minus the B-versus-C effect (the Bucher method). Randomization is kept within each trial, but A and B were never randomized against each other.",
    caution: "It is an inference that holds only if the two trials do not differ in anything that changes the treatment effects. It is not head-to-head evidence and not a ranking.",
  },
  transitivity: {
    term: "Transitivity",
    definition:
      "The assumption behind an indirect comparison: the trials are similar enough in everything that modifies the treatment effects that the A-versus-C and B-versus-C effects can be chained through C.",
    caution: "With two trials and summary data it cannot be tested, only argued from what is reported.",
  },
  "effect-modifier": {
    term: "Effect modifier",
    definition:
      "A characteristic that changes how large a treatment's effect is, such as baseline weight, diabetes status or background therapy. If two trials differ in one, their effects relative to a common comparator are not exchangeable.",
  },
  "evidence-gap": {
    term: "Evidence gap",
    definition: "A question the public record does not answer yet. A gap in public reporting is not confirmation that the sponsor has no data.",
  },
  "priority-score": {
    term: "Priority score",
    definition: "A weighted sum of four judgments scored from 0 to 1: clinical importance, uncertainty, tractability and whether action is possible now. It orders the list; it is not a measurement.",
  },
  "pending-human-review": {
    term: "Pending human review",
    definition: "A stored record for which no automatic support could be established. It waits for a reviewer to compare it with its source and is not to be relied on yet.",
  },
  "review-hold": {
    term: "Review hold",
    definition: "A record kept out of every analysis because a reviewer rejected an earlier version of it and the new version has not been decided yet.",
  },
  "orphaned-decision": {
    term: "Orphaned decision",
    definition: "A recorded review decision whose evidence record no longer exists, for example after the evidence was rebuilt from a changed source.",
    caution: "An orphaned rejection means the value it excluded may be in use again until someone decides on it.",
  },
  "source-conflict": {
    term: "True source conflict",
    definition:
      "Two or more public sources report the same quantity (same study, randomized group, endpoint, time point, estimand and baseline) and their values differ by more than the precision the sources printed.",
    caution: "A difference explained by a stated estimand, baseline, analysis set or follow-up is not a conflict. No source wins because of its kind.",
  },
  "source-change": {
    term: "Source change",
    definition: "A difference detected between two stored versions of a tracked source, such as a registry record whose status, enrollment or outcomes changed. It waits for someone to judge whether it affects the evidence.",
  },
  "tracked-development-stage": {
    term: "Most advanced tracked trial",
    definition: "The highest registry phase among the tracked trials in which the molecule is a study drug. It is derived from the trials on record here.",
    caution: "It is not the sponsor's declared development stage, and a trial that is not tracked here is not counted.",
  },
  "citation-marker": {
    term: "Citation marker",
    definition: "A label such as [C2] that names one block of the answer and the records and sources behind it. A model-drafted summary may cite only these markers, and each cited sentence is checked against the record behind its marker.",
  },
  "rule-based-interpretation": {
    term: "Rule-based interpretation",
    definition: "Text written by fixed rules from the stored records, for example whether two results may be compared and what may not be concluded. No language model takes part.",
  },
  "claim-verification": {
    term: "Claim verification",
    definition:
      "A deterministic check of a model-drafted summary: each sentence is read into structured claims (drug, arm, trial, endpoint, value, time point, estimand and so on) and compared with the record behind the marker it cites. A draft is shown only if every sentence is supported.",
  },
} as const satisfies Record<string, GlossaryEntry>;

export type GlossaryKey = keyof typeof GLOSSARY;
