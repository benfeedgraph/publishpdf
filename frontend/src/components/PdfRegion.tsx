import { useEffect, useRef } from "react";

/** A PDF page image with a highlighted bounding box (PDF points, top-left origin),
 * scrolled so the box is in view. */
export default function PdfRegion({
  src, pageWidth, pageHeight, bbox, height = 260, label,
}: {
  src: string;
  pageWidth: number;
  pageHeight: number;
  bbox?: [number, number, number, number] | number[] | null;
  height?: number;
  label?: string;
}) {
  const box = useRef<HTMLDivElement>(null);
  const wrap = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = box.current, w = wrap.current;
    if (!el || !w) return;
    const center = () => {
      w.scrollTop = el.offsetTop - w.clientHeight / 2 + el.offsetHeight / 2;
      w.scrollLeft = el.offsetLeft - w.clientWidth / 2 + el.offsetWidth / 2;
    };
    center();
    const img = w.querySelector("img");
    img?.addEventListener("load", center);
    return () => img?.removeEventListener("load", center);
  }, [src, bbox]);
  const pad = 3;
  const style = bbox
    ? {
        left: `${((bbox[0] - pad) / pageWidth) * 100}%`,
        top: `${((bbox[1] - pad) / pageHeight) * 100}%`,
        width: `${((bbox[2] - bbox[0] + 2 * pad) / pageWidth) * 100}%`,
        height: `${((bbox[3] - bbox[1] + 2 * pad) / pageHeight) * 100}%`,
      }
    : undefined;
  return (
    <div className="pdf-region" ref={wrap} style={{ height }}>
      <div className="pdf-page" style={{ width: Math.round(pageWidth * 2) }}>
        <img src={src} alt={label ?? "PDF page"} width={Math.round(pageWidth * 2)} />
        {style && <div className="pdf-hl" ref={box} style={style} aria-label="Highlighted location" />}
      </div>
    </div>
  );
}
