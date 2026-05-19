import React from "react";
import { createRoot } from "react-dom/client";
import devContent from "./dev-content.json";

type DevContent = {
  activeId: string;
  items: Record<string, { headline: string; body: string }>;
};

function App() {
  const content = devContent as DevContent;
  const activeItem = content.items[content.activeId] ?? content.items.hero;
  return (
    <main>
      <h1>{activeItem.headline}</h1>
      <p>{activeItem.body}</p>
    </main>
  );
}

const root = document.getElementById("root");
if (root === null) {
  throw new Error("Root element was not found");
}

createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
