// A deliverable's stage in one line (A06.1, CP20.2; decision 0083).
import type { MapDeliverable } from "./types";

const PAPER_STATUS_TEXT: Record<string, string> = {
  planning: "Planning",
  drafting: "Drafting",
  internal_review: "Internal review",
  ready_for_submission: "Ready for submission",
  submitted: "Submitted",
  revision: "Revision",
  accepted: "Accepted",
  withdrawn: "Withdrawn",
};

/** "Evidence mapped · next: Verified", with the paper's own status when the derived steps are not
 * all reached yet (review cp20-2-a F11: a submitted paper never reads only "Not framed"). */
export function stageSummary(d: MapDeliverable): string {
  const reached = d.steps.filter((s) => s.reached).pop();
  const next = d.steps.find((s) => !s.reached);
  const status = PAPER_STATUS_TEXT[d.paper_status] ?? d.paper_status;
  if (!next) return `Verified · paper status: ${status}`;
  return `${reached ? reached.label : "Not framed"} · next: ${next.label} · paper status: ${status}`;
}
