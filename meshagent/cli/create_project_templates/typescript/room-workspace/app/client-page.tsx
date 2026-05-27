"use client";

import dynamic from "next/dynamic";

const MeetingApp = dynamic(() => import("./meeting-app"), { ssr: false });

export function ClientPage() {
  return <MeetingApp />;
}
