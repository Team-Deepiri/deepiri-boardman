import { useEffect, useRef, useState } from "react";

export type AppSelectOption = { value: string; label: string };

type Props = {
  id: string;
  value: string;
  onChange: (value: string) => void;
  options: AppSelectOption[];
  /** First row with value ""; omit if every value must come from `options`. */
  emptyLabel?: string;
  disabled?: boolean;
};

export function AppSelect({ id, value, onChange, options, emptyLabel, disabled }: Props) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const selectedFromList = options.find((o) => o.value === value);
  let displayLabel: string;
  if (value === "" && emptyLabel !== undefined) {
    displayLabel = emptyLabel;
  } else if (selectedFromList) {
    displayLabel = selectedFromList.label;
  } else {
    displayLabel = value || emptyLabel || "";
  }

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  const pick = (next: string) => {
    onChange(next);
    setOpen(false);
  };

  return (
    <div className="app-select" ref={rootRef}>
      <button
        type="button"
        id={id}
        disabled={disabled}
        className={`app-select__trigger field__input field__select${open ? " app-select__trigger--open" : ""}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={`${id}-listbox`}
        onClick={() => !disabled && setOpen((o) => !o)}
      >
        <span className="app-select__value">{displayLabel}</span>
        <span className="app-select__chevron" aria-hidden />
      </button>
      {open ? (
        <ul
          id={`${id}-listbox`}
          role="listbox"
          className="app-select__list ui-scroll--translucent"
        >
          {emptyLabel !== undefined ? (
            <li
              role="option"
              className="app-select__option"
              aria-selected={value === ""}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pick("")}
            >
              {emptyLabel}
            </li>
          ) : null}
          {options.map((o) => (
            <li
              key={o.value}
              role="option"
              className="app-select__option"
              aria-selected={value === o.value}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pick(o.value)}
            >
              {o.label}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
