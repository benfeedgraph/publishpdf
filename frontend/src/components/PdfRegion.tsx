import { useEffect, useRef, useState } from "react";

/** A PDF page image with a highlighted bounding box (PDF points, top-left origin), scrolled
 * so the box is in view. The page fits the pane's width (no sideways scrolling); "Zoom in"
 * shows it at twice the size for small print. */
export default function PdfRegion({
  src, pageWidth, pageHeight, bbox, height = 300, label,
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
  const [zoom, setZoom] = useState(false);
  useEffect(() => {
    const el = box.current, w = wrap.current;
    if (!el || !w) return;
    const center = () => {
      w.scrollTop = el.offsetTop - w.clientHeight / 2 + el.offsetHeight / 2;
      w.scrollLeft = zoom ? el.offsetLeft - w.clientWidth / 2 + el.offsetWidth / 2 : 0;
    };
    center();
    const img = w.querySelector("img");
    img?.addEventListener("load", center);
    return () => img?.removeEventListener("load", center);
  }, [src, bbox, zoom]);
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
    <div className="pdf-region-wrap">
      <div className={`pdf-region ${zoom ? "zoomed" : ""}`} ref={wrap} style={{ height }}>
        <div className="pdf-page" style={{ width: zoom ? "200%" : "100%", aspectRatio: `${pageWidth} / ${pageHeight}` }}>
          <img src={src} alt={label ?? "PDF page"} />
          {style && <div className="pdf-hl" ref={box} style={style} aria-label="Highlighted location" />}
        </div>
      </div>
      <button type="button" className="link small" onClick={() => setZoom((z) => !z)}>{zoom ? "Fit to width" : "Zoom in"}</button>
    </div>
  );
}
