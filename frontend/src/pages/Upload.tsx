import { useParams } from "react-router-dom";
import { Dropzone } from "../components/UploadFlow";

export default function Upload() {
  const { tenantId } = useParams();
  return (
    <div className="stack">
      <div>
        <h1>Upload a report</h1>
        <p className="muted">Drop the PDF below. We'll read its details, you confirm them, and processing starts.</p>
      </div>
      <Dropzone tenantId={tenantId!} />
    </div>
  );
}
