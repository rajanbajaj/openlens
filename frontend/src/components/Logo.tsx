interface LogoProps {
  /** Header (compact) or landing-hero (large) sizing. */
  size?: 'header' | 'hero';
}

/**
 * OpenLens brand lockup: the open-ring lens mark (navy ring with a gap,
 * teal focus point + glass highlight) beside the Open(light)/Lens(bold)
 * wordmark in navy. Pure inline SVG + text, no network fonts.
 */
export function LogoMark({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 120 120" role="img" aria-label="OpenLens mark">
      <circle
        cx="52"
        cy="52"
        r="36"
        fill="none"
        stroke="#14213D"
        strokeWidth="10"
        strokeLinecap="round"
        strokeDasharray="186 40.2"
        transform="rotate(-55 52 52)"
      />
      <line
        x1="79"
        y1="79"
        x2="104"
        y2="104"
        stroke="#14213D"
        strokeWidth="10"
        strokeLinecap="round"
      />
      <circle cx="52" cy="52" r="14" fill="#0E9F8E" />
      <circle cx="86" cy="22" r="6" fill="#0E9F8E" />
    </svg>
  );
}

export function Logo({ size = 'header' }: LogoProps) {
  const mark = size === 'hero' ? 72 : 30;
  return (
    <span className={`ol-logo ol-logo-${size}`}>
      <LogoMark size={mark} />
      <span className="ol-wordmark" aria-label="OpenLens">
        <span className="ol-wordmark-open">Open</span>
        <span className="ol-wordmark-lens">Lens</span>
      </span>
    </span>
  );
}
