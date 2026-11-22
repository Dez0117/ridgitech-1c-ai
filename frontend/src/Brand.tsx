export function BrandMark({ className = '' }: { className?: string }) {
  return <span className={'mark ' + className} aria-hidden="true">Р</span>;
}

export function Brand() {
  return <div className="brand"><BrandMark /><span>Ridgitech AI</span></div>;
}
