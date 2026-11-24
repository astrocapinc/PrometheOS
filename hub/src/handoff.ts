// In-memory hand-off from the command palette to the page it opens (a note to capture, a work
// item title, a search query). Never written to the URL or to storage.
import { useEffect, useRef, useState } from "react";

export type HandoffTarget = "capture" | "work" | "knowledge" | "library";

const pending = new Map<HandoffTarget, string>();
const mounted = new Map<HandoffTarget, (text: string) => void>();

/** Give text to a page: straight to its form when it is already open, else when it mounts. */
export function handOff(target: HandoffTarget, text: string) {
  const apply = mounted.get(target);
  if (apply) apply(text);
  else pending.set(target, text);
}

/** The text handed to this page when it mounted; `apply` receives later hand-offs while it
 *  stays open (the palette used on the page it targets). */
export function useHandoff(target: HandoffTarget, apply?: (text: string) => void): string {
  const [text] = useState(() => pending.get(target) ?? "");
  const latest = useRef(apply);
  latest.current = apply;
  useEffect(() => {
    pending.delete(target);
    const receive = (value: string) => latest.current?.(value);
    mounted.set(target, receive);
    return () => {
      if (mounted.get(target) === receive) mounted.delete(target);
    };
  }, [target]);
  return text;
}
