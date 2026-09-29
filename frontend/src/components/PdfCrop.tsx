/** A tight crop of a PDF page around one value (PDF points, top-left origin), with a
 * little of the row either side for context and the value itself outlined. Pure CSS
 * cropping of the page image, so every crop on a page shares one image download. */
export default function PdfCrop({
  src, pageWidth, bbox, label, mx = 70, my = 9,
}: {
  src: string;
  pageWidth: number;
  bbox: [number, number, number, number] | number[];
  label?: string;
  mx?: number;
  my?: number;
}) {
  const [x0, y0, x1, y1] = bbox;
  const cx0 = Math.max(0, x0 - mx), cy0 = Math.max(0, y0 - my);
  const cw = Math.min(pageWidth, x1 + mx) - cx0;
  const ch = y1 + my - cy0;
  const pct = (v: number, of: number) => `${(v / of) * 100}%`;
  return (
    <div className="pdf-crop" style={{ aspectRatio: `${cw} / ${ch}` }} role="img" aria-label={label ?? "The value in the PDF"}>
      <img src={src} alt="" loading="lazy" decoding="async"
        style={{ width: pct(pageWidth, cw), left: `-${pct(cx0, cw)}`, top: `-${pct(cy0, ch)}` }} />
      <span className="pdf-crop-hl" style={{ left: pct(x0 - 2 - cx0, cw), top: pct(y0 - 2 - cy0, ch), width: pct(x1 - x0 + 4, cw), height: pct(y1 - y0 + 4, ch) }} />
    </div>
  );
}
