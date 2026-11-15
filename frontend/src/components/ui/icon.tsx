export function Icon({ name, className = "" }: { name: "archive"; className?: string }) {
  return <span aria-hidden="true" className={`icon icon-${name} ${className}`} />;
}
