import { useEffect, useId, useRef } from 'react';
import type { ReactNode } from 'react';
import { X } from 'lucide-react';

interface ModalProps {
  title: string;
  onClose: () => void;
  locked?: boolean;
  children: ReactNode;
}

export function Modal({ title, onClose, locked = false, children }: ModalProps) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();

  useEffect(() => {
    const element = dialog.current;
    if (element && !element.open) element.showModal();
  }, []);

  return (
    <dialog className="modal" ref={dialog} aria-labelledby={titleId} onClose={onClose}
      onCancel={event => { if (locked) event.preventDefault(); }}>
      <header className="modal-header">
        <h2 id={titleId}>{title}</h2>
        <button className="icon-button" type="button" aria-label="Закрыть" disabled={locked}
          onClick={() => dialog.current?.close()}><X size={20} /></button>
      </header>
      {children}
    </dialog>
  );
}
