// The hub's icon set: 24 px stroke icons drawn for PrometheOS (no icon dependency, no
// inline style; colour follows currentColor). Icons are decorative: the text beside them, or
// an aria-label on an icon-only control, carries the meaning.

const PATHS = {
  now: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3 2" />
    </>
  ),
  work: (
    <>
      <rect x="4" y="4" width="16" height="16" rx="3" />
      <path d="m8.5 12 2.5 2.5 4.5-5" />
    </>
  ),
  notebook: (
    <>
      <path d="M5 4.5h10.5A3.5 3.5 0 0 1 19 8v11.5H8.5A3.5 3.5 0 0 1 5 16z" />
      <path d="M5 16a3.5 3.5 0 0 1 3.5-3.5H19" />
    </>
  ),
  knowledge: (
    <>
      <circle cx="6" cy="7" r="2.2" />
      <circle cx="18" cy="6" r="2.2" />
      <circle cx="12" cy="17.5" r="2.2" />
      <path d="M8 8.2l2.8 7.4M16.6 7.8l-3.4 7.8M8.2 7h7.6" />
    </>
  ),
  papers: (
    <>
      <path d="M7 3.5h7l4 4V20a.5.5 0 0 1-.5.5h-10A.5.5 0 0 1 7 20z" />
      <path d="M14 3.5v4h4M9.5 12h6M9.5 15.5h6" />
    </>
  ),
  library: <path d="M5 4v16M9.5 4v16M14 4.5l5 15" />,
  matrix: (
    <>
      <rect x="4" y="4" width="16" height="16" rx="2" />
      <path d="M4 9.5h16M4 14.5h16M10 4v16" />
    </>
  ),
  approvals: (
    <>
      <path d="M12 3.5 19 6v5.5c0 4.3-3 7.6-7 9-4-1.4-7-4.7-7-9V6z" />
      <path d="m9 12 2 2 4-4" />
    </>
  ),
  runs: <path d="M3.5 12h3.5l2.5-6 5 12 2.5-6h3.5" />,
  adoption: (
    <path d="M4 8V5.5A1.5 1.5 0 0 1 5.5 4H8M16 4h2.5A1.5 1.5 0 0 1 20 5.5V8M20 16v2.5a1.5 1.5 0 0 1-1.5 1.5H16M8 20H5.5A1.5 1.5 0 0 1 4 18.5V16M7 12h10" />
  ),
  artifacts: (
    <>
      <path d="M12 3.5 20 8v8l-8 4.5L4 16V8z" />
      <path d="M4 8l8 4.5L20 8M12 12.5v8" />
    </>
  ),
  skills: (
    <>
      <path d="M14.5 4.5a4 4 0 0 0-4.8 5.3L4.5 15v4.5H9l5.2-5.2a4 4 0 0 0 5.3-4.8l-2.6 2.6-2.8-.8-.8-2.8z" />
    </>
  ),
  ai: (
    <>
      <rect x="6" y="6" width="12" height="12" rx="2.5" />
      <path d="M9.5 2.5V6M14.5 2.5V6M9.5 18v3.5M14.5 18v3.5M2.5 9.5H6M2.5 14.5H6M18 9.5h3.5M18 14.5h3.5" />
      <path d="M10 10h4v4h-4z" />
    </>
  ),
  recovery: (
    <>
      <path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3L4.5 9" />
      <path d="M4.5 4.5V9H9" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  search: (
    <>
      <circle cx="11" cy="11" r="6.5" />
      <path d="m16 16 4 4" />
    </>
  ),
  moon: <path d="M19.5 14.5A8 8 0 0 1 9.5 4.5a8 8 0 1 0 10 10z" />,
  sun: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4" />
    </>
  ),
  monitor: (
    <>
      <rect x="3.5" y="4.5" width="17" height="11.5" rx="2" />
      <path d="M8.5 20h7M12 16v4" />
    </>
  ),
  plug: <path d="M9 3.5V8M15 3.5V8M7 8h10v3a5 5 0 0 1-10 0zM12 16v4.5" />,
  signout: (
    <>
      <path d="M14 4.5H6.5a1.5 1.5 0 0 0-1.5 1.5v12a1.5 1.5 0 0 0 1.5 1.5H14" />
      <path d="M10.5 12H20M16.5 8.5 20 12l-3.5 3.5" />
    </>
  ),
  keyboard: (
    <>
      <rect x="2.5" y="6" width="19" height="12" rx="2" />
      <path d="M6.5 10h1M10.5 10h1M14.5 10h1M6.5 14h11" />
    </>
  ),
  arrow: <path d="M5 12h14M13 6l6 6-6 6" />,
  chevron: <path d="m9 6 6 6-6 6" />,
  close: <path d="M6 6l12 12M18 6 6 18" />,
  inbox: (
    <>
      <path d="M4 13.5 6.5 5h11l2.5 8.5V19H4z" />
      <path d="M4 13.5h4.5l1 2h5l1-2H20" />
    </>
  ),
  copy: (
    <>
      <rect x="8.5" y="8.5" width="11" height="11" rx="2" />
      <path d="M15.5 8.5V6a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5" />
    </>
  ),
  check: <path d="m5 12.5 4.5 4.5L19 7.5" />,
  more: (
    <>
      <circle cx="6" cy="12" r="1" />
      <circle cx="12" cy="12" r="1" />
      <circle cx="18" cy="12" r="1" />
    </>
  ),
  alert: (
    <>
      <path d="M12 4 21 19.5H3z" />
      <path d="M12 10v4.5M12 17v.01" />
    </>
  ),
} as const;

export type IconName = keyof typeof PATHS | "flame";

export function Icon({ name, size = 16, className }: { name: IconName; size?: number; className?: string }) {
  if (name === "flame") {
    return (
      <svg
        className={className ? `icon ${className}` : "icon"}
        width={size}
        height={size}
        viewBox="0 0 24 24"
        aria-hidden="true"
        focusable="false"
      >
        <path
          fill="currentColor"
          d="M12.2 2.5c.6 3.2 4.8 5.2 4.8 10.3a5 5 0 0 1-10 0c0-2.4 1.2-3.9 2.4-5 .1 1.7.9 2.8 2 3.3-.6-3.5.2-6.3.8-8.6z"
        />
      </svg>
    );
  }
  return (
    <svg
      className={className ? `icon ${className}` : "icon"}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}
