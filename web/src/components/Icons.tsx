export function Mark({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 28 28" aria-hidden="true">
      <circle cx="14" cy="14" r="14" fill="#ff007a" />
      <path d="M10 7v8a4 4 0 0 0 8 0v-1" stroke="#fff" strokeWidth="2.6" strokeLinecap="round" fill="none" />
      <circle cx="18" cy="12" r="1.6" fill="#fff" />
    </svg>
  );
}

export function Chevron({ size = 12 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 12 12" aria-hidden="true">
      <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.8" fill="none" strokeLinecap="round" />
    </svg>
  );
}

export function ArrowDown() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
      <path d="M8 2v12M3 9l5 5 5-5" stroke="currentColor" strokeWidth="2" fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function ArrowRight() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
      <path d="M3 8h10M9 4l4 4-4 4" stroke="currentColor" strokeWidth="2" fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function SearchIcon({ size = 15 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" aria-hidden="true">
      <circle cx="7" cy="7" r="5" stroke="currentColor" strokeWidth="1.8" fill="none" />
      <path d="M11 11l3 3" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

export function StarIcon({ size = 20, filled = false }: { size?: number; filled?: boolean }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" aria-hidden="true">
      <path
        d="M10 3l2.2 4.6 5 .7-3.6 3.5.9 5L10 14.4 5.5 16.8l.9-5L2.8 8.3l5-.7z"
        stroke="currentColor"
        strokeWidth="1.6"
        fill={filled ? "currentColor" : "none"}
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function HomeIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
      <path d="M3 9l7-6 7 6v8H3z" stroke="currentColor" strokeWidth="1.8" fill="none" strokeLinejoin="round" />
    </svg>
  );
}

export function DockSearchIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
      <circle cx="9" cy="9" r="5.5" stroke="currentColor" strokeWidth="1.8" fill="none" />
      <path d="M13 13l4 4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

export function LeadersIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
      <path d="M4 16V9M10 16V4M16 16v-5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}
