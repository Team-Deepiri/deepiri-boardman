/** Inline SVG icons only (no emoji, no icon fonts). */

type IconProps = { className?: string; title?: string };

export function IconBoard({ className, title }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden={title ? undefined : true}>
      {title ? <title>{title}</title> : null}
      <path
        d="M4 5.5h16v11H4v-11z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path d="M4 8.5h16" stroke="currentColor" strokeWidth="1.5" />
      <path
        d="M8 19.5h8M12 16.5v3"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
      <circle cx="8" cy="12" r="1" fill="currentColor" />
      <circle cx="12" cy="12" r="1" fill="currentColor" />
      <circle cx="16" cy="12" r="1" fill="currentColor" />
    </svg>
  );
}

export function IconSend({ className, title }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden={title ? undefined : true}>
      {title ? <title>{title}</title> : null}
      <path
        d="M4 12L20 4 14 20l-3-7-7-1z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function IconChat({ className, title }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden={title ? undefined : true}>
      {title ? <title>{title}</title> : null}
      <path
        d="M5 5h14a1 1 0 011 1v9a1 1 0 01-1 1h-5l-4 3v-3H5a1 1 0 01-1-1V6a1 1 0 011-1z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function IconClose({ className, title }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden={title ? undefined : true}>
      {title ? <title>{title}</title> : null}
      <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

export function IconUser({ className, title }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden={title ? undefined : true}>
      {title ? <title>{title}</title> : null}
      <circle cx="12" cy="8" r="3.5" stroke="currentColor" strokeWidth="1.5" />
      <path
        d="M5.5 20.5v-1c0-3 2.5-5.5 6.5-5.5s6.5 2.5 6.5 5.5v1"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function IconAgent({ className, title }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden={title ? undefined : true}>
      {title ? <title>{title}</title> : null}
      <path
        d="M12 3l2.2 4.5L19 8.5l-3.5 3.4.8 4.9L12 14.9 7.7 16.8l.8-4.9L5 8.5l4.8-1L12 3z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path d="M9 18h6M12 16v4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

export function IconRepo({ className, title }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden={title ? undefined : true}>
      {title ? <title>{title}</title> : null}
      <ellipse cx="12" cy="6" rx="8" ry="3" stroke="currentColor" strokeWidth="1.5" />
      <path
        d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6"
        stroke="currentColor"
        strokeWidth="1.5"
      />
      <path
        d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"
        stroke="currentColor"
        strokeWidth="1.5"
      />
    </svg>
  );
}

export function IconSession({ className, title }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden={title ? undefined : true}>
      {title ? <title>{title}</title> : null}
      <rect x="4" y="5" width="16" height="14" rx="2" stroke="currentColor" strokeWidth="1.5" />
      <path d="M8 3v4M16 3v4M4 10h16" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

export function IconSpark({ className, title }: IconProps) {
  return (
    <svg className={className} viewBox="0 -3 24 24" fill="none" aria-hidden={title ? undefined : true}>
      {title ? <title>{title}</title> : null}
      <path
        d="M12 2l1.2 5.2L18 8.5l-4.8 2.1L12 16l-1.2-5.4L6 8.5l4.8-2.3L12 2z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </svg>
  );
}
