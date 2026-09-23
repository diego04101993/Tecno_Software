import { useId } from "react";

export type ContentRotation = 0 | 90 | 180 | 270;

export function ContentRotationField({ value, onChange }: { value: number | null; onChange: (rotation: ContentRotation) => void }) {
  const id = useId();
  return (
    <div>
      <label htmlFor={id} className="mb-2 block text-sm font-semibold text-slate-700">Giro del contenido</label>
      <select id={id} name="rotation" value={value ?? ""} onChange={(event) => onChange(Number(event.target.value) as ContentRotation)} aria-describedby={`${id}-help`}>
        {value === null ? <option value="" disabled>Sin salida primaria configurada</option> : null}
        {[0, 90, 180, 270].map((rotation) => <option key={rotation} value={rotation}>{rotation}°</option>)}
      </select>
      <p id={`${id}-help`} className="mt-2 text-xs text-slate-500">
        Giro horario respecto de la superficie entregada por el sistema operativo del Player.
        Es independiente de la orientación lógica y la resolución. Requiere un Player compatible.
      </p>
    </div>
  );
}
