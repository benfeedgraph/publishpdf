/* Duotone icons: a soft filled shape (currentColor at low opacity) under a crisp
 * outline (currentColor), so every icon takes its colour from the surrounding text. */
import type { ReactNode } from "react";

const svg = (children: ReactNode, size = 18) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7}
    strokeLinecap="round" strokeLinejoin="round" aria-hidden className="duo">{children}</svg>
);
/** The tinted back layer of a duotone icon. */
const Tint = ({ d }: { d: string }) => <path d={d} fill="currentColor" fillOpacity={0.2} stroke="none" />;

export const IconReports = () => svg(<>
  <Tint d="M7 3h7l5 5v11a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z" />
  <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5M9 13h6M9 17h4" /></>);
export const IconDocument = ({ size = 18 }: { size?: number }) => svg(<>
  <Tint d="M7 3h7l5 5v11a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z" />
  <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5M9 13h1.5M9 17h6" /></>, size);
export const IconDesign = ({ size = 18 }: { size?: number }) => svg(<>
  <Tint d="M12 22a10 10 0 1 1 10-10c0 2-1.5 3-3 3h-2a2 2 0 0 0-1.5 3.3A2 2 0 0 1 12 22z" />
  <path d="M12 22a10 10 0 1 1 10-10c0 2-1.5 3-3 3h-2a2 2 0 0 0-1.5 3.3A2 2 0 0 1 12 22z" />
  <circle cx="13.5" cy="6.5" r="1.2" fill="currentColor" /><circle cx="17.5" cy="10.5" r="1.2" fill="currentColor" />
  <circle cx="8.5" cy="7.5" r="1.2" fill="currentColor" /><circle cx="6.5" cy="12.5" r="1.2" fill="currentColor" /></>, size);
export const IconGlobe = ({ size = 18 }: { size?: number }) => svg(<>
  <Tint d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z" />
  <circle cx="12" cy="12" r="10" /><path d="M2 12h20M12 2a15 15 0 0 1 0 20M12 2a15 15 0 0 0 0 20" /></>, size);
export const IconChart = () => svg(<>
  <Tint d="M3 21V11l4 4 4-4 3 3 7-7v14z" />
  <path d="M3 3v18h18" /><path d="M7 15l4-4 3 3 6-7" /></>);
export const IconUsers = () => svg(<>
  <Tint d="M3 21v-2a4 4 0 0 1 4-4h6a4 4 0 0 1 4 4v2zM10 3a4 4 0 1 1 0 8 4 4 0 0 1 0-8z" />
  <path d="M17 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2" /><circle cx="10" cy="7" r="4" /><path d="M21 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8" /></>);
export const IconGear = () => svg(<>
  <Tint d="M12 2.8l2.1 1.2 2.4-.3 1.2 2.1 2.1 1.2-.3 2.4L20.7 12l-1.2 2.1.3 2.4-2.1 1.2-1.2 2.1-2.4-.3L12 21.2l-2.1-1.2-2.4.3-1.2-2.1-2.1-1.2.3-2.4L3.3 12l1.2-2.1-.3-2.4 2.1-1.2 1.2-2.1 2.4.3z" />
  <path d="M12 2.8l2.1 1.2 2.4-.3 1.2 2.1 2.1 1.2-.3 2.4L20.7 12l-1.2 2.1.3 2.4-2.1 1.2-1.2 2.1-2.4-.3L12 21.2l-2.1-1.2-2.4.3-1.2-2.1-2.1-1.2.3-2.4L3.3 12l1.2-2.1-.3-2.4 2.1-1.2 1.2-2.1 2.4.3z" />
  <circle cx="12" cy="12" r="3" /></>);
export const IconShield = () => svg(<>
  <Tint d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
  <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /><path d="M9 12l2 2 4-4" /></>);
export const IconList = () => svg(<>
  <Tint d="M3 4h18v16H3z" />
  <rect x="3" y="4" width="18" height="16" rx="3" /><path d="M8 9h9M8 13h9M8 17h5" /></>);
export const IconBuilding = () => svg(<>
  <Tint d="M5 21V7l8-4v18zM13 21V7l6 4v10z" />
  <path d="M3 21h18M5 21V7l8-4v18M19 21V11l-6-4" /><path d="M9 9h.01M9 13h.01M9 17h.01" /></>);
export const IconUpload = ({ size = 26 }: { size?: number }) => svg(<>
  <Tint d="M3 15h18v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12" /></>, size);
export const IconCheck = ({ size = 18 }: { size?: number }) => svg(<path d="M20 6 9 17l-5-5" strokeWidth={2.2} />, size);
export const IconCheckCircle = ({ size = 18 }: { size?: number }) => svg(<>
  <Tint d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z" />
  <circle cx="12" cy="12" r="10" /><path d="M8 12.5l2.7 2.7L16.5 9.5" /></>, size);
export const IconAlert = ({ size = 18 }: { size?: number }) => svg(<>
  <Tint d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z" />
  <circle cx="12" cy="12" r="10" /><path d="M12 7.5v5.5M12 16.5h.01" /></>, size);
export const IconClose = ({ size = 16 }: { size?: number }) => svg(<path d="M6 6l12 12M18 6 6 18" strokeWidth={2} />, size);
export const IconSparkle = ({ size = 18 }: { size?: number }) => svg(<>
  <Tint d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z" />
  <path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z" /><path d="M19 16l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z" /></>, size);
export const IconLogout = () => svg(<>
  <Tint d="M5 3h4v18H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z" />
  <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9" /></>);
