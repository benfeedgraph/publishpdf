import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { post, type VersionDetail } from "../api";
import BuildModePicker, { BUILD_MODE_LABEL, type BuildMode } from "./BuildModePicker";
import { Button } from "./Spinner";

/** How this version's page was built, and a way to build it again another way (before
 *  publishing). Reviews are kept; only the page is rebuilt and re-checked. */
export default function RebuildCard({ base, version, canChange }: { base: string; version: VersionDetail; canChange: boolean }) {
  const qc = useQueryClient();
  const current = version.build_mode ?? null;
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<BuildMode>(current ?? "layout");
  const [err, setErr] = useState<string | null>(null);
  const bo = version.build_options;
  const rebuild = useMutation({
    mutationFn: () => post(`${base}/build-mode`, { build_mode: mode, credits_shown: bo?.options.find((o) => o.mode === mode)?.max_credits ?? 0 }),
    onSuccess: () => { setOpen(false); setErr(null); qc.invalidateQueries({ queryKey: ["version", version.id] }); },
    onError: (e: Error) => setErr(e.message),
  });
  const label = current ? BUILD_MODE_LABEL[current] : "Platform defaults (uploaded before build options)";
  return (
    <div className="card rebuild-card">
      <div className="toolbar" style={{ justifyContent: "space-between" }}>
        <span><span className="muted small">This page was built with: </span><strong>{label}</strong></span>
        {canChange && bo && !open && <button type="button" className="link" onClick={() => setOpen(true)}>Build it another way</button>}
      </div>
      {open && bo && (
        <div className="stack" style={{ marginTop: 10 }}>
          <BuildModePicker data={bo} value={mode} onChange={setMode} current={current} />
          <p className="muted small" style={{ margin: 0 }}>Your reviews are kept. Only the web page is built again and re-checked.
            {mode === "full" && current !== "full" ? " Section labels are only applied when a PDF is first read, so they start with your next upload." : ""}</p>
          <div className="btn-row">
            <Button className="primary" disabled={mode === current} busy={rebuild.isPending} busyLabel="Starting the rebuild" onClick={() => rebuild.mutate()}>
              Rebuild the page: {BUILD_MODE_LABEL[mode]}
            </Button>
            <button type="button" className="secondary" onClick={() => setOpen(false)}>Cancel</button>
          </div>
          {err && <p className="error small" role="alert">{err}</p>}
        </div>
      )}
    </div>
  );
}
