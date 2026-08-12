import { useRef, useState } from "react";

/** Drag-and-drop target for a single PDF, doubling as a click-to-browse button.
 *
 *  It is a <button> wrapping a hidden file input rather than a bare <div>: a
 *  drop target alone is unreachable by keyboard, and this is the app's primary
 *  action. Drag events are only visual — the same `onFile` runs for a drop and
 *  for a pick.
 *
 *  Validation mirrors the checks in api.py's `pdf_to_rows` so an obviously wrong
 *  file is rejected here instead of after an upload round trip. */
export function Dropzone({
  onFile,
  onError,
  busy,
}: {
  onFile: (file: File) => void;
  onError: (message: string) => void;
  busy: boolean;
}) {
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  function accept(file: File | undefined) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      onError("El archivo debe ser un PDF.");
      return;
    }
    if (file.size === 0) {
      onError("El archivo está vacío.");
      return;
    }
    onFile(file);
  }

  return (
    <>
      <button
        type="button"
        className={`dropzone${over ? " over" : ""}`}
        disabled={busy}
        onClick={() => input.current?.click()}
        // Without preventDefault on dragover the browser navigates away to the
        // dropped file instead of firing onDrop.
        onDragOver={(e) => {
          e.preventDefault();
          if (!busy) setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          if (!busy) accept(e.dataTransfer.files[0]);
        }}
      >
        {busy ? (
          "Procesando el documento…"
        ) : (
          <>
            <strong>Arrastra aquí la carpeta tributaria en PDF</strong>
            <br />o haz clic para buscarla en tu equipo
          </>
        )}
      </button>
      <input
        ref={input}
        type="file"
        accept="application/pdf"
        hidden
        onChange={(e) => {
          accept(e.target.files?.[0]);
          // Reset so picking the same file twice in a row still fires onChange.
          e.target.value = "";
        }}
      />
    </>
  );
}
