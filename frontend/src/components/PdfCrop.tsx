/** A tight crop of a PDF page around one value (PDF points, top-left origin), with a
 * little of the row either side for context and the value itself outlined. Pure CSS
 * cropping of the page image, so every crop on a page shares one image download. The crop
 * always fits its box: it's scaled to the tile's width, never scrolled. */
export default function PdfCrop({
  src, pageWidth, pageHeight, bbox, label, mx = 70, my = 9,
}: {
  src: string;
  pageWidth: number;
  pageHeight?: number;
  bbox: [number, number, number, number] | number[];
  label?: string;
  mx?: number;
  my?: number;
}) {
  const ph = pageHeight ?? Number.POSITIVE_INFINITY;
  const [x0, y0, x1, y1] = bbox.map((n) => (Number.isFinite(n) ? n : 0));
  // Context either side, but never wider than the page, and enough height for a short value.
  const cx0 = Math.max(0, x0 - mx);
  const cx1 = Math.min(pageWidth, Math.max(x1 + mx, cx0 + 40));
  const cy0 = Math.max(0, y0 - my);
  const cy1 = Math.min(ph, Math.max(y1 + my, cy0 + 14));
  const cw = Math.max(1, cx1 - cx0);
  const ch = Math.max(1, cy1 - cy0);
  const pct = (v: number, of: number) => `${(v / of) * 100}%`;
  return (
    <div className="pdf-crop" style={{ aspectRatio: `${cw} / ${ch}` }} role="img" aria-label={label ?? "The value in the PDF"}>
      <img src={src} alt="" loading="lazy" decoding="async"
        style={{ width: pct(pageWidth, cw), left: `-${pct(cx0, cw)}`, top: `-${pct(cy0, ch)}` }} />
      <span className="pdf-crop-hl" style={{ left: pct(x0 - 2 - cx0, cw), top: pct(y0 - 2 - cy0, ch), width: pct(x1 - x0 + 4, cw), height: pct(y1 - y0 + 4, ch) }} />
    </div>
  );
}
